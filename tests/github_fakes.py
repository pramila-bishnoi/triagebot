"""An in-memory fake of the GitHub REST endpoints TriageBot reads.

Plugged into :class:`app.github_client.GitHubClient` through
``httpx.MockTransport`` so no test ever touches the network.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx

_counter = itertools.count(1)


def make_issue(
    title: str,
    body: str = "",
    *,
    number: int | None = None,
    state: str = "open",
    labels: list[str] | None = None,
    reactions: int = 0,
    comments: int = 0,
    user: str = "octocat",
    updated_at: str = "2026-09-01T00:00:00Z",
) -> dict[str, Any]:
    number = number if number is not None else next(_counter)
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "user": {"login": user},
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
        "labels": [{"name": name} for name in labels or []],
        "comments": comments,
        "reactions": {"total_count": reactions},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": updated_at,
        "closed_at": "2026-09-02T00:00:00Z" if state == "closed" else None,
    }


def make_pr(
    title: str,
    body: str = "",
    *,
    draft: bool = False,
    merged_at: str | None = None,
    requested_reviewers: int = 0,
    requested_teams: int = 0,
    additions: int | None = None,
    deletions: int | None = None,
    changed_files: int | None = None,
    reviews: list[tuple[str, str]] | None = None,
    created_at: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """A PR as the issues API returns it, plus ``_pull`` data served by the pulls endpoints.

    ``reviews``: (reviewer, state) tuples in submission order.
    """
    item = make_issue(title, body, **kwargs)
    if created_at:
        item["created_at"] = created_at
    item["html_url"] = item["html_url"].replace("/issues/", "/pull/")
    item["pull_request"] = {"url": "https://api.github.com/...", "merged_at": merged_at}
    item["draft"] = draft
    item["_pull"] = {
        "requested_reviewers": [{"login": f"r{i}"} for i in range(requested_reviewers)],
        "requested_teams": [{"slug": f"t{i}"} for i in range(requested_teams)],
        "additions": additions,
        "deletions": deletions,
        "changed_files": changed_files,
        "commits": 1,
        "review_comments": 0,
        "reviews": [
            {
                "user": {"login": who},
                "state": state,
                "submitted_at": f"2026-09-0{min(i + 1, 9)}T00:00:00Z",
            }
            for i, (who, state) in enumerate(reviews or [])
        ],
    }
    return item


def _pull_object(item: dict[str, Any]) -> dict[str, Any]:
    extra = item.get("_pull", {})
    return {
        "number": item["number"],
        "title": item["title"],
        "state": item["state"],
        "draft": item.get("draft", False),
        "user": item["user"],
        "merged_at": item["pull_request"].get("merged_at"),
        "head": {"ref": f"feature-{item['number']}"},
        "base": {"ref": "main"},
        "author_association": "CONTRIBUTOR",
        "requested_reviewers": extra.get("requested_reviewers", []),
        "requested_teams": extra.get("requested_teams", []),
        "additions": extra.get("additions"),
        "deletions": extra.get("deletions"),
        "changed_files": extra.get("changed_files"),
        "commits": extra.get("commits"),
        "review_comments": extra.get("review_comments"),
    }


class FakeGitHub:
    """Serves ``GET /repos/{owner}/{name}`` and ``GET /repos/{owner}/{name}/issues``."""

    def __init__(self) -> None:
        self.repos: dict[str, dict[str, Any]] = {}
        self.issues: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []
        self.fail_with: httpx.Response | None = None
        self.raise_exc: Exception | None = None
        self.rate_limit_remaining = 59
        self.server_time: str | None = None  # value of the Date header, if set

    def add_repo(
        self,
        full_name: str,
        items: list[dict[str, Any]],
        *,
        private: bool = False,
        description: str = "A test repo",
    ) -> None:
        self.repos[full_name.lower()] = {
            "full_name": full_name,
            "html_url": f"https://github.com/{full_name}",
            "description": description,
            "private": private,
        }
        self.issues[full_name.lower()] = items

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.fail_with is not None:
            return self.fail_with
        if request.method != "GET":
            return httpx.Response(405, json={"message": "read-only fake"})

        headers = {"x-ratelimit-remaining": str(self.rate_limit_remaining)}
        if self.server_time:
            headers["date"] = self.server_time
        parts = request.url.path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "repos":
            return httpx.Response(404, json={"message": "Not Found"}, headers=headers)
        full_name = f"{parts[1]}/{parts[2]}".lower()
        if full_name not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"}, headers=headers)
        if len(parts) == 3:
            return httpx.Response(200, json=self.repos[full_name], headers=headers)
        if parts[3] == "pulls":
            return self._pulls(full_name, parts[4:], request, headers)
        if parts[3:] != ["issues"]:
            return httpx.Response(404, json={"message": "Not Found"}, headers=headers)

        state = request.url.params.get("state", "open")
        per_page = int(request.url.params.get("per_page", "30"))
        page = int(request.url.params.get("page", "1"))
        items = [i for i in self.issues[full_name] if state == "all" or i["state"] == state]
        since = request.url.params.get("since")
        if since:  # like GitHub: updated at or after `since`, ordered by update time
            items = [i for i in items if i["updated_at"] >= since]
            items.sort(
                key=lambda i: i["updated_at"],
                reverse=request.url.params.get("direction", "desc") == "desc",
            )
        start = (page - 1) * per_page
        chunk = items[start : start + per_page]
        if start + per_page < len(items):
            next_url = request.url.copy_merge_params({"page": str(page + 1)})
            headers["link"] = f'<{next_url}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    def _pulls(
        self, full_name: str, rest: list[str], request: httpx.Request, headers: dict[str, str]
    ) -> httpx.Response:
        prs = {i["number"]: i for i in self.issues[full_name] if "pull_request" in i}
        if not rest:  # GET /pulls — list (no size fields in the real list endpoint)
            state = request.url.params.get("state", "open")
            listed = [
                _pull_object(i) for i in prs.values() if state == "all" or i["state"] == state
            ]
            for pull in listed:
                for key in (
                    "additions",
                    "deletions",
                    "changed_files",
                    "commits",
                    "review_comments",
                ):
                    pull.pop(key)
            return self._page(listed, request, headers)
        number = int(rest[0]) if rest[0].isdigit() else None
        if number not in prs:
            return httpx.Response(404, json={"message": "Not Found"}, headers=headers)
        if len(rest) == 1:
            return httpx.Response(200, json=_pull_object(prs[number]), headers=headers)
        if rest[1:] == ["reviews"]:
            return self._page(prs[number].get("_pull", {}).get("reviews", []), request, headers)
        return httpx.Response(404, json={"message": "Not Found"}, headers=headers)

    @staticmethod
    def _page(items: list, request: httpx.Request, headers: dict[str, str]) -> httpx.Response:
        per_page = int(request.url.params.get("per_page", "30"))
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * per_page
        if start + per_page < len(items):
            next_url = request.url.copy_merge_params({"page": str(page + 1)})
            headers = {**headers, "link": f'<{next_url}>; rel="next"'}
        return httpx.Response(200, json=items[start : start + per_page], headers=headers)
