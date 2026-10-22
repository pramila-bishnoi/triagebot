"""Import issues and pull requests from GitHub, classify them, and store them.

Flow: fetch repo metadata -> page through issues/PRs -> normalise each raw
GitHub object -> run :func:`app.triage.classify_item` -> upsert into SQLite.
Everything is fetched before anything is written, so a failure part-way
through (e.g. a rate limit) leaves the database unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app import repository
from app.github_client import GitHubClient, GitHubError, RateLimitError
from app.ml.service import classify as classify_category
from app.pr_insights import summarize_reviews

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportSummary:
    repo: dict
    fetched: int
    issues: int
    pull_requests: int
    rate_limit_remaining: int | None
    pr_metadata: str | None = None  # outcome of the pulls-list sync, for the user


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
        # PRs only: the issues API includes the merge date at no extra cost.
        "merged_at": (raw.get("pull_request") or {}).get("merged_at") if is_pr else None,
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
        is_draft=item.get("is_draft", False),
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
        "priority_source": result.priority_source,
    }


def _github_now(client: GitHubClient) -> str:
    """GitHub's current time (from the last response), or the local clock as a fallback."""
    moment = client.server_time or datetime.now(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def import_repo(
    client: GitHubClient, owner: str, name: str, *, state: str = "open", limit: int = 100
) -> ImportSummary:
    meta = client.get_repo(owner, name)
    started_at = _github_now(client)
    raw_items = list(client.iter_issues(owner, name, state=state, limit=limit))
    items = [classify(normalize_item(raw)) for raw in raw_items]

    # One transaction: the repository row, its items and PR merge dates appear together.
    repo = repository.save_import(
        full_name=meta.get("full_name") or f"{owner}/{name}",
        html_url=meta.get("html_url") or f"https://github.com/{owner}/{name}",
        description=meta.get("description") or "",
        items=items,
        started_at=started_at,
    )

    pull_requests = sum(item["kind"] == "pr" for item in items)
    pr_metadata = None
    if pull_requests:
        pr_metadata = sync_pr_list(
            client, repo["id"], owner, name, state=state, limit=pull_requests
        )
    return ImportSummary(
        repo=repository.get_repo(repo["id"]),  # type: ignore[arg-type]
        fetched=len(items),
        issues=len(items) - pull_requests,
        pull_requests=pull_requests,
        rate_limit_remaining=client.rate_limit_remaining,
        pr_metadata=pr_metadata,
    )


# Re-read this much before the recorded sync point, to absorb clock differences
# between GitHub and timestamps recorded locally (costs a few repeated items).
SYNC_OVERLAP = timedelta(minutes=5)


def _minus_overlap(timestamp: str) -> str:
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (moment - SYNC_OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SyncSummary:
    repo: dict
    since: str
    fetched: int
    added: int
    updated: int
    closed: int
    reopened: int
    complete: bool
    synced_through: str
    rate_limit_remaining: int | None
    pr_metadata: str | None = None


def sync_repo(client: GitHubClient, repo: dict, *, limit: int = 500) -> SyncSummary:
    """Bring an imported repository up to date with changes made on GitHub.

    Fetches every issue and PR (open or closed) updated since the repository
    was last known to be current, re-classifies it and stores it — so issues
    closed or reopened on GitHub change state here too. Nothing is deleted.
    Everything is fetched before anything is written, and the write is one
    transaction: a failed sync leaves the stored data and the sync point
    unchanged. If more than ``limit`` items changed, the sync point advances
    only as far as the items applied and ``complete`` is False.
    """
    owner, name = repo["full_name"].split("/", 1)
    window_start = repository.sync_window_start(repo["id"])
    if window_start is None:
        raise ValueError("Nothing to synchronise yet: import the repository first.")
    since = _minus_overlap(window_start)
    raw_items, complete = client.fetch_updated_since(owner, name, since=since, limit=limit)
    # GitHub's time at the *last* response: each keyset request returned everything
    # updated between its cursor and the moment it was answered, so a complete
    # sync has seen every change made before this time.
    finished_at = _github_now(client)

    latest: dict[int, dict] = {}  # an item updated twice during the sync: keep the newest
    for raw in raw_items:
        latest[int(raw["number"])] = raw
    items = [classify(normalize_item(raw)) for raw in latest.values()]
    if complete:
        through = finished_at
    else:  # oldest-first order: everything updated before the last item has been applied
        through = max(str(raw.get("updated_at")) for raw in raw_items)
    stats = repository.save_sync(repo["id"], items, synced_through=through)

    pr_metadata = None
    if any(item["kind"] == "pr" for item in items):
        open_prs = repository.count_open_prs(repo["id"])
        if open_prs:
            pr_metadata = sync_pr_list(
                client, repo["id"], owner, name, state="open", limit=open_prs
            )
    return SyncSummary(
        repo=repository.get_repo(repo["id"]),  # type: ignore[arg-type]
        since=since,
        fetched=len(items),
        complete=complete,
        synced_through=through,
        rate_limit_remaining=client.rate_limit_remaining,
        pr_metadata=pr_metadata,
        **stats,
    )


def sync_pr_list(
    client: GitHubClient, repo_id: int, owner: str, name: str, *, state: str, limit: int
) -> str:
    """Fetch the pulls list (1 request per 100 PRs). Never fails the import."""
    try:
        pulls = list(client.iter_pulls(owner, name, state=state, limit=limit))
    except GitHubError as exc:
        log.warning("pulls list not synced: %s", exc)
        return f"review requests not loaded: {exc}"
    matched = repository.save_pr_list(repo_id, pulls)
    return f"review requests and branches loaded for {matched} PRs"


def fetch_pr_details(
    client: GitHubClient,
    repo: dict,
    *,
    limit: int = 10,
    only_open: bool = True,
    force: bool = False,
) -> dict:
    """Load size and review data for up to ``limit`` PRs (2 GitHub requests each).

    PRs whose data is already current are skipped. Stops cleanly on a rate limit;
    everything fetched before that is kept.
    """
    owner, name = repo["full_name"].split("/", 1)
    todo, up_to_date = repository.prs_needing_details(
        repo["id"], limit=limit, only_open=only_open, force=force
    )
    fetched, failed, stopped = 0, 0, None
    for pr in todo:
        try:
            pull = client.get_pull(owner, name, pr["number"])
            reviews = client.list_reviews(owner, name, pr["number"])
        except RateLimitError as exc:
            stopped = str(exc)
            break
        except GitHubError as exc:
            log.warning("details for PR #%s not loaded: %s", pr["number"], exc)
            failed += 1
            continue
        repository.save_pr_details(
            pr["id"],
            {
                "additions": pull.get("additions"),
                "deletions": pull.get("deletions"),
                "changed_files": pull.get("changed_files"),
                "commits": pull.get("commits"),
                "review_comments": pull.get("review_comments"),
                **summarize_reviews(reviews),
            },
        )
        fetched += 1
    return {
        "repo": repo["full_name"],
        "requested": len(todo),
        "fetched": fetched,
        "up_to_date": up_to_date,
        "failed": failed,
        "stopped_by_rate_limit": stopped is not None,
        "message": stopped,
        "rate_limit_remaining": client.rate_limit_remaining,
    }
