"""API tests for POST /repos/import, GET /repos and GET /items (GitHub is faked)."""

from __future__ import annotations

import httpx
import pytest

from tests.github_fakes import make_issue, make_pr


@pytest.fixture()
def widgets(fake_github):
    fake_github.add_repo(
        "acme/widgets",
        [
            make_issue(
                "App crashes on save for all users", "Traceback attached", number=1, labels=["bug"]
            ),
            make_issue("Fix typo in README", number=2, state="closed"),
            make_issue("How do I configure the proxy?", number=3),
            make_pr("feat: add CSV export", number=4),
            make_pr("docs: clarify install steps", number=5, draft=True),
        ],
    )
    return fake_github


def _import(client, repo="acme/widgets", **extra):
    return client.post("/repos/import", json={"repo": repo, **extra})


def test_import_counts_and_stores(client, widgets):
    resp = _import(client, state="all")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["fetched"] == 5
    assert data["issues"] == 3
    assert data["pull_requests"] == 2
    assert data["rate_limit_remaining"] == 59
    assert data["repo"]["full_name"] == "acme/widgets"
    assert data["repo"]["item_count"] == 5
    assert client.get("/items").json()["total"] == 5


def test_import_accepts_github_url(client, widgets):
    resp = _import(client, repo="https://github.com/acme/widgets")
    assert resp.status_code == 200
    assert resp.json()["fetched"] == 4  # default state=open excludes the closed issue


def test_reimport_does_not_duplicate(client, widgets):
    _import(client, state="all")
    _import(client, state="all")
    assert client.get("/items").json()["total"] == 5
    assert len(client.get("/repos").json()) == 1


def test_import_respects_limit(client, widgets):
    assert _import(client, limit=2).json()["fetched"] == 2


def test_items_include_explanations(client, widgets):
    _import(client, state="all")
    item = client.get("/items", params={"q": "#1"}).json()["items"][0]
    assert item["kind"] == "issue"
    assert item["category"] == "bug"
    assert item["priority"] == "high"
    assert item["priority_reasons"][-1].endswith("-> high")
    assert any("GitHub label 'bug'" in r for r in item["category_reasons"])
    assert item["html_url"].endswith("/issues/1")
    assert "body" not in item  # list view stays light


@pytest.mark.parametrize(
    ("params", "expected_numbers"),
    [
        ({"type": "pr"}, {4, 5}),
        ({"type": "issue", "state": "open"}, {1, 3}),
        ({"category": "documentation"}, {2, 5}),
        ({"category": "question"}, {3}),
        ({"priority": "high"}, {1}),
        ({"state": "closed"}, {2}),
        ({"q": "csv"}, {4}),
        ({"q": "traceback"}, {1}),  # body search
        ({"repo": "ACME/widgets", "type": "pr", "category": "feature"}, {4}),
        ({"repo": "other/repo"}, set()),
    ],
)
def test_item_filters(client, widgets, params, expected_numbers):
    _import(client, state="all")
    resp = client.get("/items", params=params)
    assert resp.status_code == 200
    assert {i["number"] for i in resp.json()["items"]} == expected_numbers


def test_item_pagination(client, widgets):
    _import(client, state="all")
    page = client.get("/items", params={"limit": 2, "offset": 4}).json()
    assert page["total"] == 5
    assert page["limit"] == 2 and page["offset"] == 4
    assert len(page["items"]) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"type": "commit"},
        {"category": "chore"},
        {"priority": "urgent"},
        {"state": "merged"},
        {"limit": 0},
        {"limit": 1000},
        {"offset": -1},
    ],
)
def test_invalid_item_filters_return_422(client, params):
    assert client.get("/items", params=params).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"repo": "not a repo"},
        {"repo": "a/b", "limit": 0},
        {"repo": "a/b", "limit": 501},
        {"repo": "a/b", "state": "merged"},
        {},
    ],
)
def test_invalid_import_request_returns_422(client, fake_github, body):
    assert client.post("/repos/import", json=body).status_code == 422
    assert fake_github.requests == []  # validation happens before any GitHub call


def test_import_unknown_repo_returns_404(client):
    resp = _import(client, repo="nobody/nothing")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_import_private_repo_returns_404(client, fake_github):
    fake_github.add_repo("acme/secret", [make_issue("x")], private=True)
    resp = _import(client, repo="acme/secret")
    assert resp.status_code == 404
    assert client.get("/items").json()["total"] == 0


def test_rate_limit_returns_429_with_retry_after(client, fake_github):
    fake_github.fail_with = httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4102444800"},
    )
    resp = _import(client)
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) > 0
    assert "rate limit" in resp.json()["detail"].lower()


def test_upstream_failure_returns_502_and_stores_nothing(client, fake_github):
    fake_github.raise_exc = httpx.ConnectError("network down")
    resp = _import(client)
    assert resp.status_code == 502
    assert client.get("/repos").json() == []


def test_failure_mid_pagination_leaves_db_unchanged(client, fake_github):
    fake_github.add_repo("acme/big", [make_issue(f"I{n}", number=n) for n in range(1, 151)])
    original = fake_github.handle

    def flaky(request):
        if request.url.params.get("page") == "2":
            return httpx.Response(500, json={"message": "boom"})
        return original(request)

    fake_github.handle = flaky
    resp = _import(client, repo="acme/big", limit=150)
    assert resp.status_code == 502
    assert client.get("/items").json()["total"] == 0


def test_import_never_writes_to_github(client, widgets):
    _import(client, state="all")
    assert widgets.requests
    assert {r.method for r in widgets.requests} == {"GET"}


def test_existing_manual_issue_endpoints_still_work(client, widgets):
    _import(client)
    created = client.post("/issues", json={"title": "Manual issue"}).json()
    assert [i["id"] for i in client.get("/issues").json()] == [created["id"]]
