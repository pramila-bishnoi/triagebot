"""Phase 3 integration: priority through the ML service, import, API, DB and UI."""

from __future__ import annotations

import sqlite3

import pytest

from app import repository
from app.database import MIGRATIONS, init_db
from app.ml import service
from tests.github_fakes import make_issue, make_pr


@pytest.fixture()
def prio_repo(fake_github):
    bare = make_issue("Something odd happens", number=6)  # incomplete metadata
    for key in ("labels", "reactions", "comments", "body", "user"):
        bare.pop(key)
    fake_github.add_repo(
        "acme/widgets",
        [
            make_issue("Typo in README", number=1, labels=["P1"]),
            make_issue("Data loss in production for all users", number=2),
            make_issue("App crashes", number=3, labels=["P1", "P3"]),
            make_issue("Add dark mode", number=4, reactions=400, comments=250),
            make_pr("feat: add export", number=5, draft=True),
            bare,
        ],
    )
    return fake_github


def _import(client):
    resp = client.post("/repos/import", json={"repo": "acme/widgets", "state": "all"})
    assert resp.status_code == 200, resp.text
    return {i["number"]: i for i in client.get("/items", params={"limit": 200}).json()["items"]}


def test_import_stores_priority_and_source(client, prio_repo):
    items = _import(client)
    assert (items[1]["priority"], items[1]["priority_source"]) == ("high", "maintainer_label")
    assert (items[2]["priority"], items[2]["priority_source"]) == ("high", "rules")
    assert items[3]["priority_source"] == "rules"  # conflicting labels
    assert items[3]["priority_reasons"][0].startswith("conflicting maintainer priority labels")
    assert items[4]["priority"] == "medium"  # popularity alone
    assert items[5]["priority"] == "low"  # draft PR
    assert items[6]["priority"] == "medium" and items[6]["priority_source"] == "rules"


def test_reimport_keeps_maintainer_decision(client, prio_repo):
    _import(client)
    items = _import(client)
    assert items[1]["priority_source"] == "maintainer_label"
    assert items[1]["priority"] == "high"


def test_priority_filter_still_works(client, prio_repo):
    _import(client)
    high = client.get("/items", params={"priority": "high"}).json()
    assert {i["number"] for i in high["items"]} == {1, 2}


def test_ordering_still_priority_first(client, prio_repo):
    _import(client)
    order = [i["priority"] for i in client.get("/items").json()["items"]]
    rank = {"high": 0, "medium": 1, "low": 2}
    assert order == sorted(order, key=rank.__getitem__)


def test_triage_endpoint_returns_priority(client):
    data = client.post("/triage", json={"title": "Data loss in production"}).json()
    assert data["priority"] == "high"
    assert data["priority_source"] == "rules"
    assert data["priority_reasons"][-1].endswith("-> high")
    assert data["severity"] == "urgent"  # legacy fields unchanged


def test_triage_batch_returns_priority(client):
    data = client.post("/triage/batch", json=[{"title": "Fix typo in README"}]).json()
    assert data[0]["priority"] == "low"


def test_manual_issue_endpoints_unchanged(client):
    created = client.post("/issues", json={"title": "API returns 500"}).json()
    assert "priority" not in created
    assert created["severity"] == "high"


def test_ml_path_keeps_maintainer_priority(ml_model):
    result = service.classify("crash with traceback when saving", labels=["priority: low"])
    assert result.category_source == "ml"
    assert (result.priority, result.priority_source) == ("low", "maintainer_label")


def test_ml_category_feeds_priority(ml_model):
    result = service.classify("crash with traceback when saving file")
    assert result.category == "bug" and result.category_source == "ml"
    assert any("category 'bug' (+1)" in r for r in result.priority_reasons)


def test_pr_priority_uses_draft_state():
    assert service.classify("Refactor settings loader", kind="pr", is_draft=True).priority == "low"
    assert service.classify("Refactor settings loader", kind="pr").priority == "medium"


def test_v3_database_upgrades_to_v4(db_path):
    conn = sqlite3.connect(db_path)
    for version, sql in MIGRATIONS[:3]:
        conn.executescript(f"BEGIN;{sql}PRAGMA user_version = {version};COMMIT;")
    conn.execute("INSERT INTO repos (full_name, html_url) VALUES ('a/b', 'https://github.com/a/b')")
    conn.execute(
        "INSERT INTO items (repo_id, number, kind, title, state, html_url, created_at, "
        "updated_at, category, category_confidence, priority) VALUES "
        "(1, 7, 'issue', 'old', 'open', 'u', 't', 't', 'bug', 'medium', 'high')"
    )
    conn.commit()
    conn.close()

    init_db()

    total, items = repository.list_items()
    assert total == 1
    assert items[0]["priority"] == "high"
    assert items[0]["priority_source"] == "rules"


def test_dashboard_shows_priority_source(client):
    html = client.get("/").text
    for marker in (
        "prioritySourceChip(",
        "it.priority_source",
        "Suggested priority",
        "maintainer label",
    ):
        assert marker in html


def test_namespaced_severity_label_through_import(client, fake_github):
    fake_github.add_repo(
        "acme/widgets",
        [
            make_issue("Terminal crashes when resizing", number=1, labels=["kind/regression"]),
            make_issue("Terminal crashes when resizing", number=2, labels=["sig/security"]),
        ],
    )
    items = _import(client)
    assert items[1]["priority"] == "high"
    assert items[2]["priority"] == "medium"
    assert any("severity topic" in r for r in items[1]["priority_reasons"])
