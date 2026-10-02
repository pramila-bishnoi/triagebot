"""Tests for schema migrations and the items/repos data-access functions."""

from __future__ import annotations

import sqlite3

import pytest

from app import repository
from app.database import SCHEMA_VERSION, get_schema_version, init_db
from app.importer import classify, normalize_item
from tests.github_fakes import make_issue, make_pr


def _item(raw: dict) -> dict:
    return classify(normalize_item(raw))


def test_fresh_database_gets_latest_version(db_path):
    init_db()
    assert get_schema_version() == SCHEMA_VERSION == 3
    tables = {
        row[0]
        for row in sqlite3.connect(db_path).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"issues", "repos", "items"} <= tables


def test_init_db_is_idempotent(db_path):
    init_db()
    init_db()
    assert get_schema_version() == SCHEMA_VERSION


def test_upgrades_pre_versioning_database_without_data_loss(db_path):
    # A database created by the original app: issues table, user_version = 0.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '', severity TEXT NOT NULL,
            labels TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO issues (title, severity, owner) VALUES ('old issue', 'low', 'docs-team');
        """
    )
    conn.close()

    init_db()

    assert get_schema_version() == SCHEMA_VERSION
    assert [i["title"] for i in repository.list_issues()] == ["old issue"]


def test_upsert_repo_is_case_insensitive(db_path):
    init_db()
    first = repository.upsert_repo("Acme/Widgets", "https://github.com/Acme/Widgets", "v1")
    second = repository.upsert_repo("acme/widgets", "https://github.com/acme/widgets", "v2")
    assert first["id"] == second["id"]
    assert len(repository.list_repos()) == 1
    assert repository.list_repos()[0]["description"] == "v2"


def test_upsert_items_updates_instead_of_duplicating(db_path):
    init_db()
    repo = repository.upsert_repo("acme/widgets", "https://github.com/acme/widgets", "")
    repository.upsert_items(repo["id"], [_item(make_issue("Typo in docs", number=1))])
    repository.upsert_items(
        repo["id"], [_item(make_issue("Critical outage", number=1, state="closed"))]
    )

    total, items = repository.list_items()
    assert total == 1
    assert items[0]["title"] == "Critical outage"
    assert items[0]["state"] == "closed"
    assert items[0]["priority"] == "high"


def test_json_columns_round_trip(db_path):
    init_db()
    repo = repository.upsert_repo("acme/widgets", "https://github.com/acme/widgets", "")
    repository.upsert_items(
        repo["id"], [_item(make_pr("feat: add X", labels=["enhancement"], draft=True))]
    )
    item = repository.list_items()[1][0]
    assert item["kind"] == "pr"
    assert item["is_draft"] is True
    assert item["github_labels"] == ["enhancement"]
    assert isinstance(item["category_reasons"], list) and item["category_reasons"]
    assert item["repo"] == "acme/widgets"


@pytest.fixture()
def seeded(db_path):
    init_db()
    a = repository.upsert_repo("acme/widgets", "https://github.com/acme/widgets", "")
    b = repository.upsert_repo("acme/gadgets", "https://github.com/acme/gadgets", "")
    repository.upsert_items(a["id"], [
        _item(make_issue("App crashes on save", number=1)),
        _item(make_issue("Fix typo in README", number=2, state="closed")),
        _item(make_pr("feat: add CSV export", number=3)),
        _item(make_issue("Slow: 100% CPU_usage when idle", number=4)),
    ])
    repository.upsert_items(b["id"], [_item(make_issue("How do I install this?", number=1))])
    return a, b


def test_filters_combine(seeded):
    assert repository.list_items(filters={"repo": "acme/widgets"})[0] == 4
    assert repository.list_items(filters={"repo": "ACME/GADGETS"})[0] == 1
    assert repository.list_items(filters={"kind": "pr"})[0] == 1
    assert repository.list_items(filters={"state": "closed"})[0] == 1
    assert repository.list_items(filters={"category": "question"})[0] == 1
    total, items = repository.list_items(filters={"kind": "issue", "priority": "high"})
    assert total == 1 and items[0]["title"] == "App crashes on save"


def test_ordering_is_priority_first(seeded):
    priorities = [i["priority"] for i in repository.list_items()[1]]
    order = {"high": 0, "medium": 1, "low": 2}
    assert priorities == sorted(priorities, key=order.__getitem__)


def test_search_title_body_and_number(seeded):
    assert repository.list_items(q="csv")[0] == 1
    assert repository.list_items(q="#2")[1][0]["title"] == "Fix typo in README"
    # number search spans repos: #1 exists in both
    assert repository.list_items(q="#1")[0] == 2


def test_search_escapes_like_wildcards(seeded):
    assert repository.list_items(q="100%")[0] == 1
    assert repository.list_items(q="%")[0] == 1
    assert repository.list_items(q="_")[0] == 1  # only "CPU_usage" has a literal underscore


def test_pagination(seeded):
    total, page1 = repository.list_items(limit=2, offset=0)
    _, page2 = repository.list_items(limit=2, offset=2)
    assert total == 5
    assert len(page1) == len(page2) == 2
    assert {i["id"] for i in page1}.isdisjoint({i["id"] for i in page2})


def test_unknown_filter_rejected(seeded):
    with pytest.raises(ValueError):
        repository.list_items(filters={"title; DROP TABLE items": "x"})
