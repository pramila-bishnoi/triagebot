"""Read-only GitHub REST API client.

Only ``GET`` requests are made — this module never writes to GitHub. Errors are
translated into a small hierarchy of exceptions so the API layer can return
meaningful HTTP status codes.

Unauthenticated requests are limited by GitHub to 60 per hour per IP address.
Setting the optional ``GITHUB_TOKEN`` environment variable raises that to
5,000 per hour; a fine-grained token with *no* permissions is enough for
public repositories.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app import __version__
from app.config import get_settings

MAX_PER_PAGE = 100

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_NAME = r"[A-Za-z0-9._-]{1,100}"
_SLUG_RE = re.compile(rf"^(?P<owner>{_OWNER})/(?P<name>{_NAME}?)(?:\.git)?/?$")
# Full URLs may carry extra path segments, e.g. .../owner/name/issues/12
_URL_RE = re.compile(
    rf"^https?://(?:www\.)?github\.com/(?P<owner>{_OWNER})/(?P<name>{_NAME}?)"
    r"(?:\.git)?/?(?:[/?#].*)?$"
)


class GitHubError(Exception):
    """Base class for GitHub API failures."""


class InvalidRepoError(GitHubError, ValueError):
    """The repository reference is not a valid ``owner/name``."""


class RepoNotFoundError(GitHubError):
    """The repository does not exist, is private, or has issues disabled."""


class RateLimitError(GitHubError):
    """GitHub's primary or secondary rate limit was hit."""

    def __init__(
        self, message: str, reset_at: datetime | None = None, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.reset_at = reset_at
        self.retry_after = retry_after


class GitHubAuthError(GitHubError):
    """The configured token was rejected."""


class GitHubUnavailableError(GitHubError):
    """Network failure, timeout, or a 5xx / unexpected response from GitHub."""


def parse_repo(value: str) -> tuple[str, str]:
    """Parse ``owner/name`` or a ``https://github.com/owner/name`` URL."""
    value = value.strip()
    match = _URL_RE.match(value) if "://" in value else _SLUG_RE.match(value)
    if not match or match.group("name") in {".", ".."}:
        raise InvalidRepoError(
            "Expected a public GitHub repository as 'owner/name' or "
            "'https://github.com/owner/name'."
        )
    return match.group("owner"), match.group("name")


class GitHubClient:
    """Minimal synchronous client for the endpoints TriageBot needs."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"triagebot/{__version__}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url, headers=headers, timeout=timeout, transport=transport
        )
        self.rate_limit_remaining: int | None = None
        # GitHub's clock (from the Date header of the latest response). Sync
        # cursors use it so a skewed local clock cannot cause missed updates.
        self.server_time: datetime | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- low level ---------------------------------------------------------

    def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise GitHubUnavailableError("GitHub did not respond in time.") from exc
        except httpx.RequestError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc

        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.isdigit():
            self.rate_limit_remaining = int(remaining)
        date = response.headers.get("date")
        if date:
            try:
                self.server_time = parsedate_to_datetime(date).astimezone(UTC)
            except (TypeError, ValueError):
                pass

        if response.is_success:
            return response
        self._raise_for_status(response)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = ""

        if status in (403, 429):
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                raise RateLimitError(
                    "GitHub secondary rate limit hit; slow down and retry.",
                    retry_after=int(retry_after),
                )
            if response.headers.get("x-ratelimit-remaining") == "0" or status == 429:
                reset = response.headers.get("x-ratelimit-reset")
                reset_at = (
                    datetime.fromtimestamp(int(reset), tz=UTC)
                    if reset and reset.isdigit()
                    else None
                )
                hint = (
                    ""
                    if response.request.headers.get("authorization")
                    else (" Set GITHUB_TOKEN to raise the limit from 60 to 5,000 requests/hour.")
                )
                raise RateLimitError(f"GitHub API rate limit exceeded.{hint}", reset_at=reset_at)
        if status == 401:
            raise GitHubAuthError("GitHub rejected the configured GITHUB_TOKEN.")
        if status == 404:
            raise RepoNotFoundError("Repository not found (or it is private).")
        if status == 410:
            raise RepoNotFoundError("Issues are disabled for this repository.")
        if status == 403:
            raise GitHubAuthError(f"GitHub refused the request: {message or 'forbidden'}")
        raise GitHubUnavailableError(
            f"Unexpected GitHub response {status}: {message or response.reason_phrase}"
        )

    # -- endpoints -----------------------------------------------------------

    def get_repo(self, owner: str, name: str) -> dict[str, Any]:
        data = self._get(f"/repos/{owner}/{name}").json()
        if data.get("private"):
            raise RepoNotFoundError("Only public repositories can be imported.")
        return data

    def iter_issues(
        self, owner: str, name: str, *, state: str = "open", limit: int = 100
    ) -> Iterator[dict[str, Any]]:
        """Yield up to ``limit`` issues *and* pull requests, most recently updated first.

        GitHub's issues endpoint returns both; PRs carry a ``pull_request`` key.
        Pagination follows the ``Link: rel="next"`` header.
        """
        url: str | None = f"/repos/{owner}/{name}/issues"
        params: dict[str, Any] | None = {
            "state": state,
            "sort": "updated",
            "direction": "desc",
            "per_page": min(MAX_PER_PAGE, limit),
        }
        yielded = 0
        while url and yielded < limit:
            response = self._get(url, params=params)
            page = response.json()
            if not isinstance(page, list):
                raise GitHubUnavailableError("Unexpected response shape from GitHub.")
            for raw in page:
                yield raw
                yielded += 1
                if yielded >= limit:
                    return
            next_link = response.links.get("next")
            url = next_link["url"] if next_link else None
            params = None  # the "next" URL already carries the query string

    def fetch_updated_since(
        self, owner: str, name: str, *, since: str, limit: int = 500
    ) -> tuple[list[dict[str, Any]], bool]:
        """Issues and PRs in any state updated at or after ``since``, oldest update first.

        Returns ``(items, complete)``; ``complete`` is False when ``limit`` was
        reached before the end. Uses *keyset* pagination: every request asks
        for the first page with ``since`` set to the last ``updated_at`` seen,
        so an item updated while we page moves to the end instead of shifting
        another item onto a page we already read. Overlapping items at the
        boundary are dropped; an item updated again appears again (newer data).
        """
        seen: set[tuple[int, str]] = set()
        items: list[dict[str, Any]] = []
        cursor, page_no = since, 1
        while True:
            response = self._get(
                f"/repos/{owner}/{name}/issues",
                params={
                    "state": "all",
                    "sort": "updated",
                    "direction": "asc",
                    "since": cursor,
                    "per_page": MAX_PER_PAGE,
                    "page": page_no,
                },
            )
            page = response.json()
            if not isinstance(page, list):
                raise GitHubUnavailableError("Unexpected response shape from GitHub.")
            for raw in page:
                key = (int(raw["number"]), str(raw.get("updated_at")))
                if key in seen:
                    continue
                if len(items) >= limit:
                    return items, False
                seen.add(key)
                items.append(raw)
            if len(page) < MAX_PER_PAGE:
                return items, True
            last = str(page[-1].get("updated_at") or cursor)
            # A full page that all shares one timestamp cannot move the cursor;
            # step to the next page of that timestamp instead of looping.
            cursor, page_no = (last, 1) if last != cursor else (cursor, page_no + 1)

    # -- pull requests (read-only) -------------------------------------------------

    def _paginate(
        self, url: str, params: dict[str, Any] | None, limit: int
    ) -> Iterator[dict[str, Any]]:
        yielded = 0
        next_url: str | None = url
        while next_url and yielded < limit:
            response = self._get(next_url, params=params)
            page = response.json()
            if not isinstance(page, list):
                raise GitHubUnavailableError("Unexpected response shape from GitHub.")
            for raw in page:
                yield raw
                yielded += 1
                if yielded >= limit:
                    return
            link = response.links.get("next")
            next_url = link["url"] if link else None
            params = None

    def iter_pulls(
        self, owner: str, name: str, *, state: str = "open", limit: int = 100
    ) -> Iterator[dict[str, Any]]:
        """Pull requests from the pulls list (branches, pending review requests)."""
        params = {
            "state": state,
            "sort": "updated",
            "direction": "desc",
            "per_page": min(MAX_PER_PAGE, limit),
        }
        yield from self._paginate(f"/repos/{owner}/{name}/pulls", params, limit)

    def get_pull(self, owner: str, name: str, number: int) -> dict[str, Any]:
        """One pull request with size fields (additions, deletions, changed_files)."""
        return self._get(f"/repos/{owner}/{name}/pulls/{number}").json()

    def list_reviews(
        self, owner: str, name: str, number: int, limit: int = 300
    ) -> list[dict[str, Any]]:
        """Submitted reviews of a pull request, oldest first."""
        return list(
            self._paginate(
                f"/repos/{owner}/{name}/pulls/{number}/reviews", {"per_page": MAX_PER_PAGE}, limit
            )
        )


def get_github_client() -> Iterator[GitHubClient]:
    """FastAPI dependency; overridden in tests with a mock transport."""
    settings = get_settings()
    client = GitHubClient(
        token=settings.github_token,
        base_url=settings.github_api_url,
        timeout=settings.github_timeout,
    )
    try:
        yield client
    finally:
        client.close()
