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


def make_pr(title: str, body: str = "", *, draft: bool = False, **kwargs: Any) -> dict[str, Any]:
    item = make_issue(title, body, **kwargs)
    item["html_url"] = item["html_url"].replace("/issues/", "/pull/")
    item["pull_request"] = {"url": "https://api.github.com/...", "merged_at": None}
    item["draft"] = draft
    return item


class FakeGitHub:
    """Serves ``GET /repos/{owner}/{name}`` and ``GET /repos/{owner}/{name}/issues``."""

    def __init__(self) -> None:
        self.repos: dict[str, dict[str, Any]] = {}
        self.issues: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []
        self.fail_with: httpx.Response | None = None
        self.raise_exc: Exception | None = None
        self.rate_limit_remaining = 59

    def add_repo(self, full_name: str, items: list[dict[str, Any]], *,
                 private: bool = False, description: str = "A test repo") -> None:
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
        parts = request.url.path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "repos":
            return httpx.Response(404, json={"message": "Not Found"}, headers=headers)
        full_name = f"{parts[1]}/{parts[2]}".lower()
        if full_name not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"}, headers=headers)
        if len(parts) == 3:
            return httpx.Response(200, json=self.repos[full_name], headers=headers)
        if parts[3:] != ["issues"]:
            return httpx.Response(404, json={"message": "Not Found"}, headers=headers)

        state = request.url.params.get("state", "open")
        per_page = int(request.url.params.get("per_page", "30"))
        page = int(request.url.params.get("page", "1"))
        items = [i for i in self.issues[full_name] if state == "all" or i["state"] == state]
        start = (page - 1) * per_page
        chunk = items[start:start + per_page]
        if start + per_page < len(items):
            next_url = request.url.copy_merge_params({"page": str(page + 1)})
            headers["link"] = f'<{next_url}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)
