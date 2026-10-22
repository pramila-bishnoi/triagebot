"""Tests for POST /repos/sync: keeping stored issue and PR states current (GitHub is faked)."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from app.github_client import GitHubClient
from tests.github_fakes import FakeGitHub, make_issue, make_pr

IMPORT_TIME = "Thu, 01 Oct 2026 12:00:00 GMT"  # GitHub's clock during the import
SYNC_TIME = "Fri, 02 Oct 2026 09:00:00 GMT"  # ... and during the sync
CHANGED = "2026-10-02T08:00:00Z"  # when items changed on GitHub


@pytest.fixture()
def repo(fake_github):
    fake_github.add_repo(
        "acme/widgets",
        [
            make_issue("App crashes on save", number=1),
            make_issue("Add dark mode", number=2),
            make_issue("Old closed bug", number=3, state="closed"),
            make_pr("feat: add export", number=4),
        ],
    )
    fake_github.server_time = IMPORT_TIME
    return fake_github


def _import(client, **extra):
    resp = client.post("/repos/import", json={"repo": "acme/widgets", **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _sync(client, **extra):
    return client.post("/repos/sync", json={"repo": "acme/widgets", **extra})


def _change(fake: FakeGitHub, number: int, **fields) -> None:
    item = next(i for i in fake.issues["acme/widgets"] if i["number"] == number)
    item.update({"updated_at": CHANGED, **fields})


def _states(client) -> dict[int, str]:
    return {i["number"]: i["state"] for i in client.get("/items").json()["items"]}


# --- State changes --------------------------------------------------------------------


def test_issue_closed_on_github_is_closed_after_sync(client, repo):
    _import(client)  # default state=open
    assert _states(client)[1] == "open"
    _change(repo, 1, state="closed", closed_at=CHANGED)
    repo.server_time = SYNC_TIME

    resp = _sync(client)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (data["closed"], data["reopened"], data["complete"]) == (1, 0, True)
    assert data["since"] == "2026-10-01T11:55:00Z"  # import time minus the 5-minute overlap
    assert data["synced_through"] == "2026-10-02T09:00:00Z"
    assert data["repo"]["last_synced_at"] is not None
    assert _states(client)[1] == "closed"


def test_issue_reopened_on_github_is_open_after_sync(client, repo):
    _import(client, state="all")
    assert _states(client)[3] == "closed"
    _change(repo, 3, state="open", closed_at=None)
    repo.server_time = SYNC_TIME

    data = _sync(client).json()

    assert (data["closed"], data["reopened"]) == (0, 1)
    assert _states(client)[3] == "open"


def test_sync_reclassifies_changed_items(client, repo):
    _import(client)
    _change(repo, 2, title="Critical security outage in production for all users")
    repo.server_time = SYNC_TIME
    _sync(client)
    item = next(i for i in client.get("/items").json()["items"] if i["number"] == 2)
    assert item["priority"] == "high"


def test_new_items_are_added(client, repo):
    _import(client)
    repo.issues["acme/widgets"].append(make_issue("Brand new bug", number=9, updated_at=CHANGED))
    repo.server_time = SYNC_TIME
    data = _sync(client).json()
    assert (data["added"], data["fetched"]) == (1, 1)
    assert 9 in _states(client)


def test_pr_merged_on_github_shows_as_merged(client, repo):
    _import(client)
    _change(repo, 4, state="closed")
    pr = next(i for i in repo.issues["acme/widgets"] if i["number"] == 4)
    pr["pull_request"]["merged_at"] = CHANGED
    repo.server_time = SYNC_TIME

    data = _sync(client).json()

    assert data["closed"] == 1
    prs = {p["number"]: p for p in client.get("/prs").json()["items"]}
    assert prs[4]["status"] == "merged"


# --- Nothing changed ------------------------------------------------------------------


def test_reimporting_unchanged_items_changes_nothing(client, repo):
    first = _import(client, state="all")
    before = client.get("/items").json()["items"]
    second = _import(client, state="all")
    after = client.get("/items").json()["items"]
    assert first["repo"]["item_count"] == second["repo"]["item_count"] == 4
    strip = [{k: v for k, v in i.items() if k != "duplicate_count"} for i in before]
    assert strip == [{k: v for k, v in i.items() if k != "duplicate_count"} for i in after]


def test_sync_with_no_changes_fetches_nothing_and_advances(client, repo):
    _import(client)
    repo.server_time = SYNC_TIME
    data = _sync(client).json()
    assert (data["fetched"], data["added"], data["updated"]) == (0, 0, 0)
    assert data["complete"] is True
    assert data["synced_through"] == "2026-10-02T09:00:00Z"
    # The next sync starts from there.
    assert _sync(client).json()["since"] == "2026-10-02T08:55:00Z"


def test_a_later_import_does_not_move_the_sync_point_forward(client, repo):
    """state=open re-imports skip closed items, so they cannot vouch for them."""
    _import(client)
    repo.server_time = SYNC_TIME
    second = _import(client)
    assert second["repo"]["synced_through"] == "2026-10-01T12:00:00Z"


# --- Failures leave everything unchanged ----------------------------------------------


def _synced_through(db_path) -> str:
    return sqlite3.connect(db_path).execute("SELECT synced_through FROM repos").fetchone()[0]


def test_failed_sync_stores_nothing(client, repo, db_path):
    _import(client)
    _change(repo, 1, state="closed")
    repo.fail_with = httpx.Response(500, json={"message": "boom"})

    resp = _sync(client)

    assert resp.status_code == 502
    assert _states(client)[1] == "open"
    assert _synced_through(db_path) == "2026-10-01T12:00:00Z"


def test_rate_limited_sync_returns_429_and_stores_nothing(client, repo, db_path):
    _import(client)
    _change(repo, 1, state="closed")
    repo.fail_with = httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1900000000"},
    )

    resp = _sync(client)

    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert _states(client)[1] == "open"
    assert _synced_through(db_path) == "2026-10-01T12:00:00Z"


def test_failure_on_a_later_page_stores_nothing(client, fake_github, db_path):
    fake_github.add_repo("acme/widgets", [make_issue(f"I{n}", number=n) for n in range(1, 6)])
    fake_github.server_time = IMPORT_TIME
    _import(client)
    for n in range(1, 151):
        fake_github.issues["acme/widgets"].append(
            make_issue(
                f"New {n}",
                number=100 + n,
                updated_at=f"2026-10-02T{n // 60:02d}:" f"{n % 60:02d}:00Z",
            )
        )
    original, calls = fake_github.handle, []

    def flaky(request):
        if request.url.path.endswith("/issues") and request.url.params.get("since"):
            calls.append(request)
            if len(calls) == 2:
                return httpx.Response(500, json={"message": "boom"})
        return original(request)

    fake_github.handle = flaky
    assert _sync(client).status_code == 502
    assert client.get("/items").json()["total"] == 5
    assert _synced_through(db_path) == "2026-10-01T12:00:00Z"


def test_import_failure_keeps_previously_stored_items(client, repo):
    _import(client, state="all")
    repo.fail_with = httpx.Response(500, json={"message": "boom"})
    assert client.post("/repos/import", json={"repo": "acme/widgets"}).status_code == 502
    assert len(_states(client)) == 4


def test_failed_database_write_rolls_back_the_whole_import(client, repo, monkeypatch):
    from app import repository

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(repository, "_upsert_pr_basics", broken)
    with pytest.raises(sqlite3.OperationalError):
        client.post("/repos/import", json={"repo": "acme/widgets"})
    assert client.get("/repos").json() == []  # no repository row without its items
    assert client.get("/items").json()["total"] == 0


# --- Pagination and limits ------------------------------------------------------------


def _many(fake: FakeGitHub, count: int, *, same_time: bool = False) -> None:
    fake.add_repo("acme/widgets", [make_issue("Seed issue", number=1)])
    fake.server_time = IMPORT_TIME
    for n in range(count):
        stamp = (
            "2026-10-02T05:00:00Z"
            if same_time
            else f"2026-10-02T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"
        )
        fake.issues["acme/widgets"].append(
            make_issue(f"Changed {n}", number=1000 + n, updated_at=stamp, state="closed")
        )


def test_sync_pages_through_all_changes(client, fake_github):
    _many(fake_github, 250)
    _import(client)
    fake_github.server_time = SYNC_TIME
    data = _sync(client).json()
    assert (data["fetched"], data["added"], data["complete"]) == (250, 250, True)
    sync_requests = [r for r in fake_github.requests if r.url.params.get("since")]
    assert all(r.url.params["direction"] == "asc" for r in sync_requests)
    assert len(sync_requests) == 3


def test_limit_stops_early_and_the_next_sync_continues(client, fake_github):
    _many(fake_github, 250)
    _import(client)
    fake_github.server_time = SYNC_TIME

    first = _sync(client, limit=100).json()
    assert (first["fetched"], first["complete"]) == (100, False)
    assert first["synced_through"] == "2026-10-02T00:01:39Z"  # the 100th change applied

    second = _sync(client, limit=1000).json()
    assert second["complete"] is True
    assert client.get("/items").json()["total"] == 251


def test_many_items_with_the_same_timestamp_do_not_loop(client, fake_github):
    _many(fake_github, 150, same_time=True)
    _import(client)
    fake_github.server_time = SYNC_TIME
    data = _sync(client).json()
    assert (data["fetched"], data["complete"]) == (150, True)


def test_item_updated_during_paging_is_not_skipped():
    """Keyset pagination: an update between pages cannot shift an item past us."""
    fake = FakeGitHub()
    fake.add_repo(
        "acme/widgets",
        [
            make_issue(f"I{n}", number=n, updated_at=f"2026-10-02T00:{n // 60:02d}:{n % 60:02d}Z")
            for n in range(150)
        ],
    )
    original = fake.handle

    def moving(request):
        response = original(request)
        if len(fake.requests) == 1:  # item 5 is edited right after the first page
            fake.issues["acme/widgets"][5]["updated_at"] = "2026-10-02T09:00:00Z"
        return response

    fake.handle = moving
    with GitHubClient(transport=httpx.MockTransport(fake.handle)) as gh:
        items, complete = gh.fetch_updated_since(
            "acme", "widgets", since="2026-10-01T00:00:00Z", limit=1000
        )
    assert complete
    assert {i["number"] for i in items} == set(range(150))


# --- Validation and safety ------------------------------------------------------------


def test_sync_unknown_or_invalid_repo(client, repo):
    assert _sync(client).status_code == 404
    assert client.post("/repos/sync", json={"repo": "not a repo"}).status_code == 422
    assert client.post("/repos/sync", json={"repo": "acme/widgets", "limit": 0}).status_code == 422


def test_sync_never_writes_to_github(client, repo):
    _import(client)
    _change(repo, 1, state="closed")
    _sync(client)
    assert {r.method for r in repo.requests} == {"GET"}


def test_repositories_imported_before_sync_tracking_use_their_oldest_refresh(client, repo, db_path):
    _import(client)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE repos SET synced_through = NULL")
    conn.execute("UPDATE items SET imported_at = '2026-09-20 10:00:00'")
    conn.commit()
    conn.close()
    assert _sync(client).json()["since"] == "2026-09-20T09:55:00Z"


def test_empty_repository_sync_completes_without_items(client, fake_github):
    fake_github.add_repo("acme/empty", [])
    fake_github.server_time = IMPORT_TIME
    imported = client.post("/repos/import", json={"repo": "acme/empty"})
    assert imported.status_code == 200, imported.text

    fake_github.server_time = SYNC_TIME
    response = client.post("/repos/sync", json={"repo": "acme/empty"})

    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["fetched"], data["added"], data["updated"], data["complete"]) == (0, 0, 0, True)
    assert data["synced_through"] == "2026-10-02T09:00:00Z"


def test_empty_legacy_repository_sync_returns_conflict(client, fake_github, db_path):
    fake_github.add_repo("acme/empty", [])
    fake_github.server_time = IMPORT_TIME
    imported = client.post("/repos/import", json={"repo": "acme/empty"})
    assert imported.status_code == 200, imported.text

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE repos SET synced_through = NULL")
    conn.commit()
    conn.close()

    response = client.post("/repos/sync", json={"repo": "acme/empty"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Nothing to synchronise yet: import the repository first."


def test_sync_reviewed_duplicate_decisions_survive(client, fake_github):
    fake_github.add_repo(
        "acme/widgets",
        [
            make_issue(
                "Export to CSV crashes with unicode filenames", "csv export unicode crash", number=1
            ),
            make_issue(
                "CSV export crashes on unicode filenames", "csv export unicode crash", number=2
            ),
        ],
    )
    fake_github.server_time = IMPORT_TIME
    _import(client)
    first = client.get("/items").json()["items"][0]
    suggestion = client.get(f"/items/{first['id']}/duplicates").json()["suggestions"][0]
    client.patch(f"/duplicates/{suggestion['id']}", json={"status": "rejected"})

    _change(fake_github, 1, body="csv export unicode crash, still happening")
    fake_github.server_time = SYNC_TIME
    data = _sync(client).json()

    assert data["duplicates"]["rejected"] == 1
    again = client.get(f"/items/{first['id']}/duplicates").json()["suggestions"][0]
    assert again["status"] == "rejected"
