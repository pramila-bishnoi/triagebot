"""Explainable pull-request insights from public GitHub metadata.

Every insight says where it comes from:

* ``github`` — a fact GitHub reports directly (draft, merged, pending review
  requests, review states, line and file counts);
* ``calculated`` — derived by TriageBot from GitHub data with a documented
  rule (age, time since last update, stale, size label, "large").

What it deliberately does **not** do: judge code quality, security, test
coverage or whether a PR can be merged. "Approved" only reports reviewers'
latest review state.

All rules are deterministic; ``now`` is passed in, so results are reproducible.
Thresholds are configurable (``TRIAGEBOT_PR_STALE_DAYS``,
``TRIAGEBOT_PR_LARGE_LINES``, ``TRIAGEBOT_PR_LARGE_FILES``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

DEFAULT_STALE_DAYS = 30  # open PR with no activity for this many days
DEFAULT_LARGE_LINES = 500  # additions + deletions
DEFAULT_LARGE_FILES = 20  # changed files

# Conventional size buckets by lines changed (additions + deletions), as used by
# common "PR size" labelling bots. Upper bounds are exclusive; not tuned on data.
SIZE_BUCKETS: list[tuple[str, int]] = [("XS", 10), ("S", 100), ("M", 500), ("L", 1000)]
SIZE_LABELS = ("XS", "S", "M", "L", "XL")

FLAGS = (
    "draft",
    "stale",
    "awaiting_review",
    "no_reviews",
    "changes_requested",
    "approved",
    "large",
)


@dataclass(frozen=True)
class Thresholds:
    stale_days: int = DEFAULT_STALE_DAYS
    large_lines: int = DEFAULT_LARGE_LINES
    large_files: int = DEFAULT_LARGE_FILES


def thresholds_from_env() -> Thresholds:
    from app.config import warn_invalid

    def positive_int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value <= 0:
            warn_invalid(name, raw, default)
            return default
        return value

    return Thresholds(
        stale_days=positive_int("TRIAGEBOT_PR_STALE_DAYS", DEFAULT_STALE_DAYS),
        large_lines=positive_int("TRIAGEBOT_PR_LARGE_LINES", DEFAULT_LARGE_LINES),
        large_files=positive_int("TRIAGEBOT_PR_LARGE_FILES", DEFAULT_LARGE_FILES),
    )


def utcnow() -> datetime:
    """Current time (patched in tests)."""
    return datetime.now(UTC)


def parse_time(value: str | None) -> datetime | None:
    """Parse GitHub ('2026-09-01T12:00:00Z') or SQLite ('2026-09-01 12:00:00') times."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def days_between(start: datetime | None, end: datetime) -> float | None:
    if start is None:
        return None
    return max(0.0, (end - start).total_seconds() / 86400)


def size_label(lines_changed: int | None) -> str | None:
    if lines_changed is None:
        return None
    for label, upper in SIZE_BUCKETS:
        if lines_changed < upper:
            return label
    return "XL"


def summarize_reviews(reviews: list[dict[str, Any]]) -> dict[str, int]:
    """Count reviewers by their *latest* decisive review, as GitHub does.

    COMMENTED reviews do not change a reviewer's approval state; DISMISSED
    clears it; PENDING (unsubmitted drafts) are ignored.
    """
    latest: dict[str, str] = {}
    total = commented = 0
    ordered = sorted(reviews, key=lambda r: r.get("submitted_at") or "")
    for review in ordered:
        state = (review.get("state") or "").upper()
        reviewer = (review.get("user") or {}).get("login") or "?"
        if state == "PENDING":
            continue
        total += 1
        if state == "COMMENTED":
            commented += 1
        elif state in ("APPROVED", "CHANGES_REQUESTED"):
            latest[reviewer] = state
        elif state == "DISMISSED":
            latest.pop(reviewer, None)
    values = list(latest.values())
    return {
        "reviews_total": total,
        "reviews_commented": commented,
        "reviews_approved": values.count("APPROVED"),
        "reviews_changes_requested": values.count("CHANGES_REQUESTED"),
    }


@dataclass
class Insight:
    code: str
    title: str
    reason: str
    source: str  # "github" | "calculated"


@dataclass
class PRInsight:
    status: str  # open | draft | merged | closed
    age_days: float | None
    idle_days: float | None
    lines_changed: int | None
    size: str | None
    flags: list[str] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)


def _plural(n: int | float, word: str) -> str:
    n = int(n)
    return f"{n} {word}{'' if n == 1 else 's'}"


def analyze(
    pr: dict[str, Any], *, now: datetime | None = None, thresholds: Thresholds | None = None
) -> PRInsight:
    """Insights for one PR row (an ``items`` row joined with ``pull_requests``).

    Missing fields are treated as "unknown": the related insight is skipped or
    reported as unavailable, never guessed.
    """
    now = now or utcnow()
    t = thresholds or Thresholds()
    insights: list[Insight] = []
    flags: list[str] = []
    is_open = pr.get("state") == "open"
    draft = bool(pr.get("is_draft"))
    merged_at = parse_time(pr.get("merged_at"))

    # Status (GitHub)
    if is_open and draft:
        status = "draft"
        flags.append("draft")
        insights.append(
            Insight(
                "draft",
                "Draft",
                "GitHub marks this PR as a draft: the " "author has not asked for review yet.",
                "github",
            )
        )
    elif is_open:
        status = "open"
    elif merged_at:
        status = "merged"
        insights.append(
            Insight(
                "merged", "Merged", f"GitHub reports it merged on {merged_at.date()}.", "github"
            )
        )
    elif not pr.get("merge_known", True):
        status = "closed"
        insights.append(
            Insight(
                "closed_unknown",
                "Closed (merge status unknown)",
                "Imported before PR data was tracked; re-import the "
                "repository to learn whether it was merged.",
                "calculated",
            )
        )
    else:
        status = "closed"
        insights.append(
            Insight(
                "closed",
                "Closed without merging",
                "GitHub reports it closed and not merged.",
                "github",
            )
        )

    # Age and activity (calculated)
    age = days_between(parse_time(pr.get("created_at")), now)
    idle = days_between(parse_time(pr.get("updated_at")), now)
    if age is not None:
        insights.append(
            Insight(
                "age",
                f"Opened {_plural(age, 'day')} ago",
                "Calculated from the creation date reported by GitHub.",
                "calculated",
            )
        )
    if is_open and idle is not None and idle >= t.stale_days:
        flags.append("stale")
        insights.append(
            Insight(
                "stale",
                f"Stale: no activity for {_plural(idle, 'day')}",
                f"Open and last updated {_plural(idle, 'day')} ago (threshold: "
                f"{t.stale_days} days). Any activity — a comment, commit or label — resets it.",
                "calculated",
            )
        )

    # Reviews (GitHub)
    requested_people, requested_teams = pr.get("requested_reviewers"), pr.get("requested_teams")
    if requested_people is not None or requested_teams is not None:
        pending = (requested_people or 0) + (requested_teams or 0)
        if is_open and not draft and pending > 0:
            flags.append("awaiting_review")
            who = ", ".join(
                x
                for x in (
                    _plural(requested_people, "reviewer") if requested_people else "",
                    _plural(requested_teams, "team") if requested_teams else "",
                )
                if x
            )
            insights.append(
                Insight(
                    "awaiting_review",
                    "Awaiting review",
                    f"GitHub lists pending review requests for {who} (a request disappears "
                    "once that reviewer submits a review).",
                    "github",
                )
            )
    reviews_total = pr.get("reviews_total")
    if reviews_total is not None:
        approved = pr.get("reviews_approved") or 0
        changes = pr.get("reviews_changes_requested") or 0
        if changes:
            flags.append("changes_requested")
            insights.append(
                Insight(
                    "changes_requested",
                    "Changes requested",
                    f"The latest review from {_plural(changes, 'reviewer')} requests changes.",
                    "github",
                )
            )
        elif approved:
            flags.append("approved")
            insights.append(
                Insight(
                    "approved",
                    f"Approved by {_plural(approved, 'reviewer')}",
                    "Latest review state only — this does not check CI, conflicts or branch "
                    "protection, so it does not mean the PR can be merged.",
                    "github",
                )
            )
        if is_open and not draft and reviews_total == 0:
            flags.append("no_reviews")
            insights.append(
                Insight(
                    "no_reviews",
                    "No reviews yet",
                    "GitHub returned no submitted reviews for this PR.",
                    "github",
                )
            )

    # Size (GitHub counts, calculated label)
    additions, deletions = pr.get("additions"), pr.get("deletions")
    files = pr.get("changed_files")
    lines = additions + deletions if additions is not None and deletions is not None else None
    label = size_label(lines)
    if lines is not None:
        insights.append(
            Insight(
                "size",
                f"Size {label}: {_plural(lines, 'line')} changed",
                f"+{additions} / −{deletions} lines in {_plural(files or 0, 'file')} (GitHub). "
                f"Label from lines changed: XS < 10, S < 100, M < 500, L < 1000, XL ≥ 1000.",
                "calculated",
            )
        )
        reasons = []
        if lines >= t.large_lines:
            reasons.append(f"{lines} lines changed ≥ {t.large_lines}")
        if files is not None and files >= t.large_files:
            reasons.append(f"{files} files changed ≥ {t.large_files}")
        if reasons:
            flags.append("large")
            insights.append(
                Insight(
                    "large",
                    "Large PR",
                    "; ".join(reasons) + ". Large PRs are usually slower to review; this says "
                    "nothing about code quality.",
                    "calculated",
                )
            )
    if pr.get("details_fetched_at") is None:
        insights.append(
            Insight(
                "details_missing",
                "Size and reviews not loaded",
                "Line counts and review states need extra GitHub requests; load them with "
                "“Load size & reviews”.",
                "calculated",
            )
        )

    return PRInsight(
        status=status,
        age_days=None if age is None else round(age, 1),
        idle_days=None if idle is None else round(idle, 1),
        lines_changed=lines,
        size=label,
        flags=flags,
        insights=insights,
    )
