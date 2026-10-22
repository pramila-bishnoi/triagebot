"""Duplicate detection through import, API, database and dashboard (GitHub is faked)."""

from __future__ import annotations

import sqlite3

import pytest

from app import duplicates, repository
from app.database import MIGRATIONS, init_db
from tests.github_fakes import make_issue, make_pr

CRASH_BODY = (
    "Clicking save on the profile settings page crashes the app with "
    "NullReferenceException in ProfileForm.save"
)


@pytest.fixture()
def dup_repo(fake_github):
    fake_github.add_repo(
        "acme/widgets",
        [
            make_issue("Settings page crashes when saving profile", CRASH_BODY, number=1),
            make_issue("Settings page crashes when saving profile", CRASH_BODY, number=2),
            make_issue(
                "Add dark mode to the documentation site",
                "It would be nice to have a dark theme for the docs website.",
                number=3,
            ),
            make_pr("Settings page crashes when saving profile", CRASH_BODY, number=4),
            make_issue("bug", "", number=5),
        ],
    )
    return fake_github


def _import(client, repo="acme/widgets"):
    resp = client.post("/repos/import", json={"repo": repo, "state": "all"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _items(client):
    return {i["number"]: i for i in client.get("/items", params={"limit": 200}).json()["items"]}


def test_import_runs_duplicate_analysis(client, dup_repo):
    result = _import(client)
    dup = result["duplicates"]
    assert dup["repo"] == "acme/widgets"
    assert dup["issues"] == 4  # the PR is not compared
    assert dup["compared"] == 3  # "bug" is too short
    assert dup["skipped_short"] == 1
    assert (dup["pending"], dup["confirmed"], dup["rejected"]) == (1, 0, 0)
    items = _items(client)
    assert items[1]["duplicate_count"] == items[2]["duplicate_count"] == 1
    assert items[3]["duplicate_count"] == items[4]["duplicate_count"] == 0


def test_item_duplicates_endpoint(client, dup_repo):
    _import(client)
    item = _items(client)[1]
    data = client.get(f"/items/{item['id']}/duplicates").json()
    assert data["number"] == 1 and data["threshold"] == duplicates.DEFAULT_THRESHOLD
    [s] = data["suggestions"]
    assert s["other"]["number"] == 2
    assert s["other"]["html_url"].endswith("/issues/2")
    assert s["score"] == pytest.approx(1.0)
    assert s["status"] == "pending" and s["decided_at"] is None
    assert any("not a probability" in line for line in s["explanation"])
    # the same pair, seen from the other issue
    other = client.get(f"/items/{s['other']['id']}/duplicates").json()
    assert other["suggestions"][0]["id"] == s["id"]
    assert other["suggestions"][0]["other"]["number"] == 1


def test_pull_request_has_note_and_no_suggestions(client, dup_repo):
    _import(client)
    pr = _items(client)[4]
    data = client.get(f"/items/{pr['id']}/duplicates").json()
    assert data["suggestions"] == []
    assert "issues only" in data["note"]


def _suggestion_id(client):
    item = _items(client)[1]
    return client.get(f"/items/{item['id']}/duplicates").json()["suggestions"][0]["id"]


def test_confirm_reject_and_undo(client, dup_repo):
    _import(client)
    sid = _suggestion_id(client)
    confirmed = client.patch(f"/duplicates/{sid}", json={"status": "confirmed"}).json()
    assert confirmed["status"] == "confirmed" and confirmed["decided_at"]
    rejected = client.patch(f"/duplicates/{sid}", json={"status": "rejected"}).json()
    assert rejected["status"] == "rejected"
    assert _items(client)[1]["duplicate_count"] == 0  # rejected ones are not counted
    undone = client.patch(f"/duplicates/{sid}", json={"status": "pending"}).json()
    assert undone["status"] == "pending" and undone["decided_at"] is None
    assert _items(client)[1]["duplicate_count"] == 1


def test_decisions_survive_reimport_and_forced_refresh(client, dup_repo):
    _import(client)
    sid = _suggestion_id(client)
    client.patch(f"/duplicates/{sid}", json={"status": "rejected"})
    _import(client)
    client.post("/duplicates/refresh", json={"repo": "acme/widgets", "force": True})
    assert repository.get_suggestion(sid)["status"] == "rejected"


def test_decision_kept_even_when_texts_diverge(client, dup_repo):
    _import(client)
    sid = _suggestion_id(client)
    client.patch(f"/duplicates/{sid}", json={"status": "confirmed"})
    dup_repo.issues["acme/widgets"][1] = make_issue(
        "Upgrade numpy dependency to 2.0", "numpy 2.0 was released; bump the minimum.", number=2
    )
    _import(client)
    assert repository.get_suggestion(sid)["status"] == "confirmed"


def test_stale_pending_suggestions_are_removed(client, dup_repo):
    _import(client)
    sid = _suggestion_id(client)
    dup_repo.issues["acme/widgets"][1] = make_issue(
        "Upgrade numpy dependency to 2.0", "numpy 2.0 was released; bump the minimum.", number=2
    )
    result = _import(client)
    assert result["duplicates"]["pending"] == 0
    assert repository.get_suggestion(sid) is None


def test_refresh_is_skipped_when_nothing_changed(client, dup_repo):
    _import(client)
    reused = client.post("/duplicates/refresh", json={"repo": "acme/widgets"}).json()
    assert reused["reused"] is True and reused["compared"] is None
    assert reused["pending"] == 1
    forced = client.post("/duplicates/refresh", json={"repo": "ACME/widgets", "force": True})
    assert forced.json()["reused"] is False and forced.json()["compared"] == 3


def test_threshold_is_configurable(client, dup_repo, monkeypatch):
    monkeypatch.setenv("TRIAGEBOT_DUP_THRESHOLD", "0.99")
    _import(client)
    data = client.post("/duplicates/refresh", json={"repo": "acme/widgets"}).json()
    assert data["threshold"] == 0.99 and data["pending"] == 1  # identical texts score 1.0
    monkeypatch.setenv("TRIAGEBOT_DUP_THRESHOLD", "not-a-number")
    assert duplicates.current_threshold() == duplicates.DEFAULT_THRESHOLD


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("post", "/duplicates/refresh", {"repo": "nobody/nothing"}, 404),
        ("post", "/duplicates/refresh", {"repo": "not a repo"}, 422),
        ("get", "/items/9999/duplicates", None, 404),
        ("patch", "/duplicates/9999", {"status": "confirmed"}, 404),
        ("patch", "/duplicates/1", {"status": "merged"}, 422),
    ],
)
def test_error_responses(client, method, path, body, status):
    kwargs = {"json": body} if body is not None else {}
    assert getattr(client, method)(path, **kwargs).status_code == status


def test_duplicate_failure_never_breaks_import(client, dup_repo, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("similarity failure")

    monkeypatch.setattr(duplicates, "analyze_repo", boom)
    result = _import(client)
    assert result["duplicates"] is None
    assert result["fetched"] == 5


def test_duplicates_do_not_change_category_or_priority(client, dup_repo, monkeypatch):
    _import(client)
    with_dups = {
        n: (
            i["category"],
            i["category_source"],
            i["priority"],
            i["priority_source"],
            i["category_reasons"],
            i["priority_reasons"],
        )
        for n, i in _items(client).items()
    }
    monkeypatch.setattr(
        duplicates, "analyze_repo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disabled"))
    )
    dup_repo.issues["acme/widgets"].append(make_issue("Unrelated new issue about fonts", number=6))
    _import(client)
    without = {
        n: (
            i["category"],
            i["category_source"],
            i["priority"],
            i["priority_source"],
            i["category_reasons"],
            i["priority_reasons"],
        )
        for n, i in _items(client).items()
        if n != 6
    }
    assert without == with_dups


def test_manual_triage_unchanged(client):
    created = client.post("/issues", json={"title": "API returns 500"}).json()
    assert created["severity"] == "high"
    assert "duplicate_count" not in created


def test_v4_database_upgrades_to_v5(db_path):
    conn = sqlite3.connect(db_path)
    for version, sql in MIGRATIONS[:4]:
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
    assert total == 1 and items[0]["duplicate_count"] == 0
    assert repository.get_repo_by_name("a/b")["duplicates_fingerprint"] is None
    tables = {
        r[0]
        for r in sqlite3.connect(db_path).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "duplicate_suggestions" in tables


def test_pair_constraints(db_path):
    init_db()
    repo = repository.upsert_repo("a/b", "https://github.com/a/b", "")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    for n in (1, 2):
        conn.execute(
            "INSERT INTO items (repo_id, number, kind, title, state, html_url, created_at, "
            "updated_at, category, category_confidence, priority) VALUES "
            f"({repo['id']}, {n}, 'issue', 't', 'open', 'u', 't', 't', 'bug', 'low', 'low')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # pairs are stored once, smaller id first
        conn.execute(
            "INSERT INTO duplicate_suggestions (repo_id, item_a, item_b, score, "
            "title_similarity) VALUES (?, 2, 1, 0.9, 0.9)",
            (repo["id"],),
        )
    with pytest.raises(sqlite3.IntegrityError):  # status is constrained
        conn.execute(
            "INSERT INTO duplicate_suggestions (repo_id, item_a, item_b, score, "
            "title_similarity, status) VALUES (?, 1, 2, 0.9, 0.9, 'merged')",
            (repo["id"],),
        )


def test_dashboard_has_duplicate_ui(client):
    html = client.get("/").text
    for marker in (
        'id="refreshDups"',
        "details.dups",
        "Potential duplicates",
        "/duplicates/refresh",
        "Confirm duplicate",
        "Not a duplicate",
        "nothing is changed on GitHub",
    ):
        assert marker in html
