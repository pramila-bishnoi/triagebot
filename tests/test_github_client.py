"""Tests for the read-only GitHub client, using a mocked transport."""

from __future__ import annotations

import httpx
import pytest

from app.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubUnavailableError,
    InvalidRepoError,
    RateLimitError,
    RepoNotFoundError,
    parse_repo,
)
from tests.github_fakes import FakeGitHub, make_issue, make_pr


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("fastapi/fastapi", ("fastapi", "fastapi")),
        ("  octo-org/my.repo_name  ", ("octo-org", "my.repo_name")),
        ("https://github.com/psf/requests", ("psf", "requests")),
        ("https://github.com/psf/requests.git", ("psf", "requests")),
        ("https://github.com/psf/requests/issues/12", ("psf", "requests")),
        ("http://www.github.com/a/b/", ("a", "b")),
    ],
)
def test_parse_repo_valid(value, expected):
    assert parse_repo(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "fastapi",
        "a/b/c d",
        "-bad/repo",
        "owner/..",
        "https://gitlab.com/a/b",
        "owner/repo name",
        "../../etc/passwd",
    ],
)
def test_parse_repo_invalid(value):
    with pytest.raises(InvalidRepoError):
        parse_repo(value)


def _client(fake: FakeGitHub, token: str | None = None) -> GitHubClient:
    return GitHubClient(token=token, transport=fake.transport())


def test_paginates_and_respects_limit():
    fake = FakeGitHub()
    fake.add_repo("acme/widgets", [make_issue(f"Issue {n}", number=n) for n in range(1, 251)])
    with _client(fake) as gh:
        items = list(gh.iter_issues("acme", "widgets", limit=230))
    assert len(items) == 230
    assert [i["number"] for i in items[:3]] == [1, 2, 3]
    assert len(fake.requests) == 3  # 100 + 100 + 30 (stops early)
    first = fake.requests[0].url.params
    assert first["per_page"] == "100"
    assert first["sort"] == "updated"


def test_small_limit_uses_small_page():
    fake = FakeGitHub()
    fake.add_repo("acme/widgets", [make_issue(f"I{n}") for n in range(20)])
    with _client(fake) as gh:
        items = list(gh.iter_issues("acme", "widgets", limit=5))
    assert len(items) == 5
    assert fake.requests[0].url.params["per_page"] == "5"
    assert len(fake.requests) == 1


def test_state_filter_is_forwarded_and_prs_included():
    fake = FakeGitHub()
    fake.add_repo("acme/widgets", [make_issue("open one"), make_pr("closed pr", state="closed")])
    with _client(fake) as gh:
        assert len(list(gh.iter_issues("acme", "widgets", state="open"))) == 1
        items = list(gh.iter_issues("acme", "widgets", state="all"))
    assert any("pull_request" in i for i in items)


def test_only_get_requests_are_made():
    fake = FakeGitHub()
    fake.add_repo("acme/widgets", [make_issue("x")])
    with _client(fake) as gh:
        gh.get_repo("acme", "widgets")
        list(gh.iter_issues("acme", "widgets"))
    assert {r.method for r in fake.requests} == {"GET"}


def test_token_header_only_when_configured():
    fake = FakeGitHub()
    fake.add_repo("acme/widgets", [])
    with _client(fake) as gh:
        gh.get_repo("acme", "widgets")
    with _client(fake, token="secret-token") as gh:
        gh.get_repo("acme", "widgets")
    assert "authorization" not in fake.requests[0].headers
    assert fake.requests[1].headers["authorization"] == "Bearer secret-token"


def test_tracks_rate_limit_remaining():
    fake = FakeGitHub()
    fake.rate_limit_remaining = 42
    fake.add_repo("acme/widgets", [])
    with _client(fake) as gh:
        gh.get_repo("acme", "widgets")
        assert gh.rate_limit_remaining == 42


def test_missing_repo_raises_not_found():
    with _client(FakeGitHub()) as gh, pytest.raises(RepoNotFoundError):
        gh.get_repo("nobody", "nothing")


def test_private_repo_rejected():
    fake = FakeGitHub()
    fake.add_repo("acme/secret", [], private=True)
    with _client(fake) as gh, pytest.raises(RepoNotFoundError, match="public"):
        gh.get_repo("acme", "secret")


def test_primary_rate_limit():
    fake = FakeGitHub()
    fake.fail_with = httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1900000000"},
    )
    with _client(fake) as gh, pytest.raises(RateLimitError) as info:
        gh.get_repo("acme", "widgets")
    assert info.value.reset_at is not None
    assert "GITHUB_TOKEN" in str(info.value)


def test_secondary_rate_limit_uses_retry_after():
    fake = FakeGitHub()
    fake.fail_with = httpx.Response(
        403, json={"message": "slow down"}, headers={"retry-after": "30"}
    )
    with _client(fake) as gh, pytest.raises(RateLimitError) as info:
        gh.get_repo("acme", "widgets")
    assert info.value.retry_after == 30


def test_429_is_rate_limit():
    fake = FakeGitHub()
    fake.fail_with = httpx.Response(429, json={"message": "too many"})
    with _client(fake) as gh, pytest.raises(RateLimitError):
        gh.get_repo("acme", "widgets")


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, GitHubAuthError),
        (403, GitHubAuthError),
        (410, RepoNotFoundError),
        (500, GitHubUnavailableError),
        (502, GitHubUnavailableError),
    ],
)
def test_http_errors_are_mapped(status, error):
    fake = FakeGitHub()
    fake.fail_with = httpx.Response(status, json={"message": "nope"})
    with _client(fake) as gh, pytest.raises(error):
        gh.get_repo("acme", "widgets")


def test_non_json_error_body():
    fake = FakeGitHub()
    fake.fail_with = httpx.Response(503, text="<html>down</html>")
    with _client(fake) as gh, pytest.raises(GitHubUnavailableError, match="503"):
        gh.get_repo("acme", "widgets")


@pytest.mark.parametrize("exc", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
def test_network_errors(exc):
    fake = FakeGitHub()
    fake.raise_exc = exc
    with _client(fake) as gh, pytest.raises(GitHubUnavailableError):
        gh.get_repo("acme", "widgets")
