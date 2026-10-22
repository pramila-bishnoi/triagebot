"""PR insights through import, database and API (GitHub is faked; time is fixed)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import httpx
import pytest

from app import pr_insights, repository
from app.database import MIGRATIONS, init_db
from tests.github_fakes import make_issue, make_pr

NOW = datetime(2026, 10, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(pr_insights, "utcnow", lambda: NOW)


@pytest.fixture()
def pr_repo(fake_github):
    fake_github.add_repo(
        "acme/widgets",
        [
            make_issue("Crash on save", number=1),
            make_pr(
                "feat: big refactor",
                number=2,
                user="alice",
                requested_reviewers=2,
                additions=900,
                deletions=300,
                changed_files=40,
                reviews=[("bob", "CHANGES_REQUESTED")],
                updated_at="2026-09-30T00:00:00Z",
            ),
            make_pr(
                "docs: typo",
                number=3,
                user="bob",
                additions=1,
                deletions=1,
                changed_files=1,
                reviews=[("carol", "APPROVED")],
                updated_at="2026-09-29T00:00:00Z",
            ),
            make_pr(
                "wip: experiment",
                number=4,
                user="alice",
                draft=True,
                updated_at="2026-07-01T00:00:00Z",
            ),
            make_pr(
                "fix: merged one",
                number=5,
                user="dave",
                state="closed",
                merged_at="2026-09-10T00:00:00Z",
                updated_at="2026-09-10T00:00:00Z",
            ),
            make_pr("old idea", number=6, user="erin", updated_at="2026-08-01T00:00:00Z"),
        ],
    )
    return fake_github


def _import(client, **extra):
    resp = client.post("/repos/import", json={"repo": "acme/widgets", "state": "all", **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _prs(client, **params):
    resp = client.get("/prs", params={"repo": "acme/widgets", **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_number(page):
    return {p["number"]: p for p in page["items"]}


def test_import_stores_github_pr_metadata(client, pr_repo):
    result = _import(client)
    assert result["pull_requests"] == 5
    assert "loaded for 5 PRs" in result["pr_metadata"]
    prs = _by_number(_prs(client))
    assert set(prs) == {2, 3, 4, 5, 6}  # issues are not PRs
    assert prs[5]["status"] == "merged" and prs[5]["merged_at"].startswith("2026-09-10")
    assert prs[4]["status"] == "draft"
    assert prs[2]["requested_reviewers"] == 2 and prs[2]["head_ref"] == "feature-2"
    assert prs[2]["additions"] is None and prs[2]["size"] is None  # details not loaded yet


def test_import_without_prs_skips_pull_requests(client, fake_github):
    fake_github.add_repo("acme/widgets", [make_issue("Only an issue", number=1)])
    result = _import(client)
    assert result["pr_metadata"] is None
    assert not any("/pulls" in r.url.path for r in fake_github.requests)
    assert _prs(client)["total"] == 0


def test_pulls_list_failure_never_breaks_import(client, pr_repo):
    original = pr_repo.handle

    def no_pulls(request):
        if request.url.path.endswith("/pulls"):
            return httpx.Response(
                403, json={"message": "rate"}, headers={"x-ratelimit-remaining": "0"}
            )
        return original(request)

    pr_repo.handle = no_pulls
    result = _import(client)
    assert result["fetched"] == 6
    assert result["pr_metadata"].startswith("review requests not loaded")
    pr = _by_number(_prs(client))[2]
    assert pr["requested_reviewers"] is None  # unknown, not 0
    assert "awaiting_review" not in pr["flags"]


def test_insights_and_flags(client, pr_repo):
    _import(client)
    prs = _by_number(_prs(client))
    assert prs[2]["flags"] == ["awaiting_review"]
    assert prs[4]["flags"] == ["draft", "stale"]
    assert prs[6]["flags"] == ["stale"]
    assert prs[6]["idle_days"] == 62.0 and prs[6]["age_days"] == 62.0
    sources = {i["code"]: i["source"] for i in prs[2]["insights"]}
    assert sources["awaiting_review"] == "github" and sources["age"] == "calculated"


def test_load_details_then_size_and_reviews(client, pr_repo):
    _import(client)
    result = client.post("/prs/details", json={"repo": "acme/widgets"}).json()
    assert (result["requested"], result["fetched"], result["failed"]) == (4, 4, 0)
    assert result["stopped_by_rate_limit"] is False
    prs = _by_number(_prs(client))
    big = prs[2]
    assert (big["lines_changed"], big["size"], big["changed_files"]) == (1200, "XL", 40)
    assert set(big["flags"]) == {"awaiting_review", "changes_requested", "large"}
    assert prs[3]["size"] == "XS" and "approved" in prs[3]["flags"]
    assert prs[5]["size"] is None  # closed: only_open=True by default


def test_details_are_cached_until_the_pr_changes(client, pr_repo):
    _import(client)
    client.post("/prs/details", json={"repo": "acme/widgets"})
    again = client.post("/prs/details", json={"repo": "acme/widgets"}).json()
    assert (again["requested"], again["up_to_date"]) == (0, 4)
    pr_repo.issues["acme/widgets"][2]["updated_at"] = "2026-10-01T00:00:00Z"  # PR #3 changed
    _import(client)
    after_change = client.post("/prs/details", json={"repo": "acme/widgets"}).json()
    assert after_change["requested"] == 1
    forced = client.post("/prs/details", json={"repo": "acme/widgets", "force": True}).json()
    assert forced["requested"] == 4


def test_details_limit_and_closed_prs(client, pr_repo):
    _import(client)
    one = client.post("/prs/details", json={"repo": "acme/widgets", "limit": 1}).json()
    assert one["fetched"] == 1
    everything = client.post(
        "/prs/details", json={"repo": "acme/widgets", "only_open": False}
    ).json()
    assert everything["fetched"] == 4  # 5 PRs, one already loaded


def test_details_stop_cleanly_on_rate_limit(client, pr_repo):
    _import(client)
    original, calls = pr_repo.handle, {"n": 0}

    def limited(request):
        if "/pulls/" in request.url.path:
            calls["n"] += 1
            if calls["n"] > 2:  # after one PR (2 requests)
                return httpx.Response(
                    403,
                    json={"message": "rate"},
                    headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4102444800"},
                )
        return original(request)

    pr_repo.handle = limited
    result = client.post("/prs/details", json={"repo": "acme/widgets"}).json()
    assert result["fetched"] == 1 and result["stopped_by_rate_limit"] is True
    assert "rate limit" in result["message"].lower()
    assert _prs(client)["summary"]["with_details"] == 1  # kept what it loaded


def test_details_skip_missing_pr(client, pr_repo):
    _import(client)
    original = pr_repo.handle

    def gone(request):
        if request.url.path.endswith("/pulls/6"):
            return httpx.Response(404, json={"message": "Not Found"})
        return original(request)

    pr_repo.handle = gone
    result = client.post("/prs/details", json={"repo": "acme/widgets"}).json()
    assert (result["fetched"], result["failed"]) == (3, 1)


def test_only_get_requests_to_github(client, pr_repo):
    _import(client)
    client.post("/prs/details", json={"repo": "acme/widgets", "only_open": False})
    assert {r.method for r in pr_repo.requests} == {"GET"}


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"status": "open"}, {2, 3, 6}),
        ({"status": "draft"}, {4}),
        ({"status": "merged"}, {5}),
        ({"author": "ALICE"}, {2, 4}),
        ({"flag": "stale"}, {4, 6}),
        ({"flag": "awaiting_review"}, {2}),
        ({"min_age_days": 1}, {2, 3, 4, 5, 6}),
        ({"max_age_days": 0.5}, set()),
        ({"q": "typo"}, {3}),
        ({"q": "#5"}, {5}),
        ({"size": "unknown"}, {2, 3, 4, 5, 6}),
    ],
)
def test_filters(client, pr_repo, params, expected):
    _import(client)
    assert set(_by_number(_prs(client, limit=200, **params))) == expected


def test_filters_after_details(client, pr_repo):
    _import(client)
    client.post("/prs/details", json={"repo": "acme/widgets"})
    assert set(_by_number(_prs(client, flag="large"))) == {2}
    assert set(_by_number(_prs(client, size="XS"))) == {3}
    assert set(_by_number(_prs(client, flag="changes_requested"))) == {2}


def test_summary_counts_ignore_status_filters(client, pr_repo):
    _import(client)
    page = _prs(client, status="merged")
    assert page["total"] == 1
    s = page["summary"]
    assert (s["total"], s["open"], s["draft"], s["merged"], s["stale"]) == (5, 3, 1, 1, 2)
    assert page["thresholds"] == {"stale_days": 30, "large_lines": 500, "large_files": 20}


def test_sorting_and_pagination(client, pr_repo):
    _import(client)
    by_idle = [p["number"] for p in _prs(client, sort="idle")["items"]]
    assert by_idle[0] == 4  # idle longest
    page = _prs(client, limit=2, offset=2)
    assert page["total"] == 5 and len(page["items"]) == 2


def test_single_pr_and_errors(client, pr_repo):
    _import(client)
    pr = _by_number(_prs(client))[2]
    assert client.get(f"/prs/{pr['id']}").json()["number"] == 2
    issue_id = client.get("/items", params={"type": "issue"}).json()["items"][0]["id"]
    assert client.get(f"/prs/{issue_id}").status_code == 404  # an issue is not a PR
    assert client.get("/prs/99999").status_code == 404
    assert client.get("/prs", params={"flag": "merge-ready"}).status_code == 422
    assert client.get("/prs", params={"sort": "random"}).status_code == 422
    assert client.post("/prs/details", json={"repo": "nobody/nothing"}).status_code == 404
    assert (
        client.post("/prs/details", json={"repo": "acme/widgets", "limit": 51}).status_code == 422
    )


def test_empty_database(client):
    page = client.get("/prs").json()
    assert page["total"] == 0 and page["summary"]["total"] == 0


def test_issue_features_and_duplicate_decisions_unaffected(client, fake_github):
    body = "Clicking save crashes the app with NullReferenceException in ProfileForm.save"
    fake_github.add_repo(
        "acme/widgets",
        [
            make_issue("Settings page crashes when saving profile", body, number=1),
            make_issue("Settings page crashes when saving profile", body, number=2),
            make_pr("feat: thing", number=3, additions=10, deletions=1, changed_files=1),
        ],
    )
    _import(client)
    items = {i["number"]: i for i in client.get("/items").json()["items"]}
    sid = client.get(f"/items/{items[1]['id']}/duplicates").json()["suggestions"][0]["id"]
    client.patch(f"/duplicates/{sid}", json={"status": "rejected"})
    before = {n: (i["category"], i["priority"]) for n, i in items.items()}
    client.post("/prs/details", json={"repo": "acme/widgets"})
    _import(client)
    after = {
        i["number"]: (i["category"], i["priority"]) for i in client.get("/items").json()["items"]
    }
    assert after == before
    assert repository.get_suggestion(sid)["status"] == "rejected"


def test_v5_database_upgrades_to_v6(db_path):
    conn = sqlite3.connect(db_path)
    for version, sql in MIGRATIONS[:5]:
        conn.executescript(f"BEGIN;{sql}PRAGMA user_version = {version};COMMIT;")
    conn.execute("INSERT INTO repos (full_name, html_url) VALUES ('a/b', 'https://github.com/a/b')")
    conn.execute(
        "INSERT INTO items (repo_id, number, kind, title, state, html_url, created_at, "
        "updated_at, category, category_confidence, priority) VALUES "
        "(1, 7, 'pr', 'old pr', 'open', 'u', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', "
        "'feature', 'low', 'medium')"
    )
    conn.commit()
    conn.close()

    init_db()

    [pr] = repository.list_prs()
    assert pr["number"] == 7 and pr["merged_at"] is None and pr["additions"] is None
    assert pr["merge_known"] is False  # imported before v6
    view = pr_insights.analyze(pr, now=NOW)
    assert view.status == "open" and "details_missing" in [i.code for i in view.insights]
    closed = pr_insights.analyze({**pr, "state": "closed"}, now=NOW)
    assert closed.insights[0].code == "closed_unknown"


def test_dashboard_has_pr_view(client):
    html = client.get("/").text
    for marker in (
        'data-view="prs"',
        'id="view-prs"',
        "/prs/details",
        "Load size",
        "prFlagChips",
        "Calculated by TriageBot",
    ):
        assert marker in html
