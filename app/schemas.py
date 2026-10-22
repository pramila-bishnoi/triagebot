"""Pydantic request/response models (the API contract)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.github_client import parse_repo

Kind = Literal["issue", "pr"]
Category = Literal["bug", "feature", "documentation", "question"]
Priority = Literal["low", "medium", "high"]
State = Literal["open", "closed"]
CategorySource = Literal["ml", "rules", "github_label"]
PrioritySource = Literal["rules", "maintainer_label"]


def _canonical_repo(value: str) -> str:
    owner, name = parse_repo(value)  # raises ValueError -> 422
    return f"{owner}/{name}"


# 'owner/name' or a GitHub URL, normalised to 'owner/name'. One definition for
# every request that names a repository.
RepoName = Annotated[str, Field(max_length=300), AfterValidator(_canonical_repo)]


class IssueIn(BaseModel):
    """An incoming issue to triage or store."""

    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(default="", max_length=5000)


class IssueUpdate(BaseModel):
    """Partial update. Any field left as ``None`` is unchanged."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=5000)
    status: str | None = Field(default=None, pattern="^(open|closed)$")


class TriageSuggestion(BaseModel):
    """The triage engine's suggestion for an issue (no persistence).

    ``severity`` / ``labels`` / ``owner`` / ``reason`` come from the original
    keyword engine; the ``category*`` fields from the ML model (with rule-based
    fallback), as used for imported issues.
    """

    severity: str
    labels: list[str]
    owner: str
    reason: str
    category: Category | None = None
    category_confidence: Literal["low", "medium", "high"] | None = None
    category_source: CategorySource | None = None
    category_score: float | None = None
    category_reasons: list[str] = Field(default_factory=list)
    priority: Priority | None = None
    priority_source: PrioritySource | None = None
    priority_reasons: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    """A triaged issue as returned by the API."""

    id: int
    title: str
    body: str
    severity: str
    labels: list[str]
    owner: str
    reason: str
    status: str
    created_at: str


class BatchTriageItem(TriageSuggestion):
    """One result within a batch-triage response."""

    title: str


# --- GitHub import -------------------------------------------------------------


class RepoImportRequest(BaseModel):
    """Which public repository to import, and how much of it."""

    repo: RepoName = Field(
        ...,
        description="'owner/name' or https://github.com/owner/name",
        examples=["fastapi/fastapi"],
    )
    state: Literal["open", "closed", "all"] = "open"
    limit: int = Field(default=100, ge=1, le=500, description="Max issues + PRs to fetch")


class Repo(BaseModel):
    id: int
    full_name: str
    html_url: str
    description: str
    last_imported_at: str | None
    item_count: int
    synced_through: str | None = Field(
        default=None, description="GitHub time up to which stored items are known to be current"
    )
    last_synced_at: str | None = Field(default=None, description="last successful sync (UTC)")


class DuplicateAnalysis(BaseModel):
    """Result of (re-)running duplicate detection for one repository."""

    repo: str
    issues: int = Field(description="issues in the repository (pull requests are not compared)")
    compared: int | None = Field(description="issues long enough to compare; null if reused")
    skipped_short: int | None = Field(description="issues skipped as too short; null if reused")
    pending: int
    confirmed: int
    rejected: int
    threshold: float = Field(description="minimum similarity score for a suggestion")
    reused: bool = Field(description="true if nothing changed since the last analysis")


class ImportResult(BaseModel):
    repo: Repo
    fetched: int
    issues: int
    pull_requests: int
    rate_limit_remaining: int | None = Field(
        description="GitHub API requests left in the current window, if reported"
    )
    duplicates: DuplicateAnalysis | None = Field(
        default=None, description="duplicate analysis run after the import (null if it failed)"
    )
    pr_metadata: str | None = Field(
        default=None, description="outcome of loading PR review requests (null if no PRs)"
    )


class RepoSyncRequest(BaseModel):
    repo: RepoName = Field(..., description="an imported repository, 'owner/name'")
    limit: int = Field(
        default=500, ge=1, le=1000, description="max changed items to apply in this run"
    )


class SyncResult(BaseModel):
    repo: Repo
    since: str = Field(description="items updated on GitHub at or after this time were fetched")
    fetched: int
    added: int = Field(description="items not stored before (created or first seen)")
    updated: int = Field(description="stored items refreshed")
    closed: int = Field(description="stored items that changed from open to closed")
    reopened: int = Field(description="stored items that changed from closed to open")
    complete: bool = Field(description="false if the limit was reached: sync again to continue")
    synced_through: str
    rate_limit_remaining: int | None
    duplicates: DuplicateAnalysis | None = None
    pr_metadata: str | None = None


class Item(BaseModel):
    """An imported issue or pull request with its triage suggestion."""

    id: int
    repo: str
    number: int
    kind: Kind
    title: str
    state: State
    author: str
    html_url: str
    github_labels: list[str]
    comments: int
    reactions: int
    is_draft: bool
    created_at: str
    updated_at: str
    closed_at: str | None
    category: Category
    category_confidence: Literal["low", "medium", "high"]
    priority: Priority
    owner_hint: str | None
    category_reasons: list[str]
    priority_reasons: list[str]
    category_source: CategorySource
    category_score: float | None = Field(
        description="ML probability of the category, when the ML model decided it"
    )
    priority_source: PrioritySource = Field(
        default="rules",
        description="'maintainer_label' if an explicit GitHub priority label set it, "
        "else 'rules' (a suggestion for human review)",
    )
    duplicate_count: int = Field(
        default=0, description="possible duplicates not rejected by a maintainer"
    )


class ItemPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[Item]


DuplicateStatus = Literal["pending", "confirmed", "rejected"]


class DuplicateRefreshRequest(BaseModel):
    repo: RepoName = Field(..., description="an imported repository, 'owner/name'")
    force: bool = Field(default=False, description="re-run even if nothing changed")


class DuplicateOther(BaseModel):
    id: int
    number: int
    title: str
    html_url: str
    state: str


class DuplicateSuggestion(BaseModel):
    """A possible duplicate of the requested item, with its explanation and review status."""

    id: int
    other: DuplicateOther
    score: float = Field(description="combined similarity 0–1 (not a probability)")
    title_similarity: float
    body_similarity: float | None
    shared_terms: list[str]
    explanation: list[str]
    status: DuplicateStatus
    decided_at: str | None


class ItemDuplicates(BaseModel):
    item_id: int
    number: int
    title: str
    threshold: float
    note: str | None = None
    suggestions: list[DuplicateSuggestion]


class DuplicateDecision(BaseModel):
    status: DuplicateStatus = Field(
        description="'confirmed' or 'rejected' records a maintainer decision; "
        "'pending' undoes it. Nothing is changed on GitHub."
    )


class DuplicateRecord(BaseModel):
    id: int
    item_a: int
    item_b: int
    score: float
    title_similarity: float
    body_similarity: float | None
    shared_terms: list[str]
    status: DuplicateStatus
    decided_at: str | None
    updated_at: str


PRStatus = Literal["open", "draft", "merged", "closed"]
PRFlag = Literal[
    "draft", "stale", "awaiting_review", "no_reviews", "changes_requested", "approved", "large"
]
PRSize = Literal["XS", "S", "M", "L", "XL", "unknown"]


class PRInsightLine(BaseModel):
    code: str
    title: str
    reason: str
    source: Literal["github", "calculated"]


class PRView(BaseModel):
    """One pull request with its metadata and explained insights."""

    id: int
    repo: str
    number: int
    title: str
    html_url: str
    author: str
    status: PRStatus
    created_at: str
    updated_at: str
    closed_at: str | None
    merged_at: str | None
    age_days: float | None = Field(description="calculated: days since creation")
    idle_days: float | None = Field(description="calculated: days since last update")
    head_ref: str | None
    base_ref: str | None
    author_association: str | None
    comments: int
    requested_reviewers: int | None = Field(description="GitHub: pending review requests")
    requested_teams: int | None
    additions: int | None
    deletions: int | None
    changed_files: int | None
    lines_changed: int | None
    size: str | None = Field(description="calculated label from lines changed (XS–XL)")
    reviews_total: int | None
    reviews_approved: int | None
    reviews_changes_requested: int | None
    details_fetched_at: str | None
    flags: list[PRFlag]
    insights: list[PRInsightLine]


class PRSummary(BaseModel):
    """Counts over the PRs matching repo/author/search (before status/flag/size/age filters)."""

    total: int
    open: int
    draft: int
    merged: int
    closed: int
    stale: int
    awaiting_review: int
    large: int
    changes_requested: int
    approved: int
    no_reviews: int
    with_details: int = Field(description="PRs whose size and reviews have been loaded")


class PRThresholds(BaseModel):
    stale_days: int
    large_lines: int
    large_files: int


class PRPage(BaseModel):
    total: int
    limit: int
    offset: int
    summary: PRSummary
    thresholds: PRThresholds
    items: list[PRView]


class PRDetailsRequest(BaseModel):
    repo: RepoName = Field(..., description="an imported repository, 'owner/name'")
    limit: int = Field(
        default=10, ge=1, le=50, description="PRs to load (2 GitHub API requests each)"
    )
    only_open: bool = True
    force: bool = Field(default=False, description="reload even if the data is current")


class PRDetailsResult(BaseModel):
    repo: str
    requested: int
    fetched: int
    up_to_date: int = Field(description="PRs skipped because their data is current")
    failed: int
    stopped_by_rate_limit: bool
    message: str | None
    rate_limit_remaining: int | None


class ModelStatus(BaseModel):
    """Whether the ML classifier is loaded, and its recorded evaluation scores."""

    available: bool
    reason: str | None = None
    threshold: float
    model: str | None = None
    classes: list[str] = Field(default_factory=list)
    trained_at: str | None = None
    train_size: int | None = None
    test_size: int | None = None
    test_accuracy: float | None = None
    test_macro_f1: float | None = None
    rules_baseline_macro_f1: float | None = None
    unseen_repo_macro_f1: float | None = None
