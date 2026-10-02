"""Import issues and pull requests from GitHub, classify them, and store them.

Flow: fetch repo metadata -> page through issues/PRs -> normalise each raw
GitHub object -> run :func:`app.triage.classify_item` -> upsert into SQLite.
Everything is fetched before anything is written, so a failure part-way
through (e.g. a rate limit) leaves the database unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app import repository
from app.github_client import GitHubClient
from app.ml.service import classify as classify_category


@dataclass(frozen=True)
class ImportSummary:
    repo: dict
    fetched: int
    issues: int
    pull_requests: int
    rate_limit_remaining: int | None


def normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a GitHub issues-API object into our ``items`` row shape (minus triage)."""
    is_pr = "pull_request" in raw
    labels = [
        label["name"] if isinstance(label, dict) else str(label)
        for label in raw.get("labels") or []
    ]
    return {
        "number": int(raw["number"]),
        "kind": "pr" if is_pr else "issue",
        "title": raw.get("title") or "",
        "body": raw.get("body") or "",
        "state": "closed" if raw.get("state") == "closed" else "open",
        "author": (raw.get("user") or {}).get("login", ""),
        "html_url": raw.get("html_url") or "",
        "github_labels": labels,
        "comments": int(raw.get("comments") or 0),
        "reactions": int((raw.get("reactions") or {}).get("total_count") or 0),
        "is_draft": bool(raw.get("draft")),
        "created_at": raw.get("created_at") or "",
        "updated_at": raw.get("updated_at") or raw.get("created_at") or "",
        "closed_at": raw.get("closed_at"),
    }


def classify(item: dict[str, Any]) -> dict[str, Any]:
    """Return ``item`` extended with category / priority / reasons."""
    result = classify_category(
        item["title"],
        item["body"],
        kind=item["kind"],
        labels=item["github_labels"],
        reactions=item["reactions"],
        comments=item["comments"],
    )
    return {
        **item,
        "category": result.category,
        "category_confidence": result.category_confidence,
        "priority": result.priority,
        "owner_hint": result.owner_hint,
        "category_reasons": result.category_reasons,
        "priority_reasons": result.priority_reasons,
        "category_source": result.category_source,
        "category_score": result.category_score,
    }


def import_repo(
    client: GitHubClient, owner: str, name: str, *, state: str = "open", limit: int = 100
) -> ImportSummary:
    meta = client.get_repo(owner, name)
    raw_items = list(client.iter_issues(owner, name, state=state, limit=limit))
    items = [classify(normalize_item(raw)) for raw in raw_items]

    repo = repository.upsert_repo(
        full_name=meta.get("full_name") or f"{owner}/{name}",
        html_url=meta.get("html_url") or f"https://github.com/{owner}/{name}",
        description=meta.get("description") or "",
    )
    repository.upsert_items(repo["id"], items)

    pull_requests = sum(1 for item in items if item["kind"] == "pr")
    return ImportSummary(
        repo=repository.get_repo(repo["id"]),  # type: ignore[arg-type]
        fetched=len(items),
        issues=len(items) - pull_requests,
        pull_requests=pull_requests,
        rate_limit_remaining=client.rate_limit_remaining,
    )
