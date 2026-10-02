"""Pydantic request/response models (the API contract)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.github_client import parse_repo

Kind = Literal["issue", "pr"]
Category = Literal["bug", "feature", "documentation", "question"]
Priority = Literal["low", "medium", "high"]
State = Literal["open", "closed"]
CategorySource = Literal["ml", "rules", "github_label"]


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

    repo: str = Field(
        ..., max_length=300, description="'owner/name' or https://github.com/owner/name",
        examples=["fastapi/fastapi"],
    )
    state: Literal["open", "closed", "all"] = "open"
    limit: int = Field(default=100, ge=1, le=500, description="Max issues + PRs to fetch")

    @field_validator("repo")
    @classmethod
    def _valid_repo(cls, value: str) -> str:
        owner, name = parse_repo(value)  # raises ValueError -> 422
        return f"{owner}/{name}"


class Repo(BaseModel):
    id: int
    full_name: str
    html_url: str
    description: str
    last_imported_at: str | None
    item_count: int


class ImportResult(BaseModel):
    repo: Repo
    fetched: int
    issues: int
    pull_requests: int
    rate_limit_remaining: int | None = Field(
        description="GitHub API requests left in the current window, if reported"
    )


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


class ItemPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[Item]


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
