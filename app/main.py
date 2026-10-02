"""FastAPI application entry point for TriageBot.

Wires together the persistence layer, the triage engine, the read-only GitHub
importer, and the HTTP API, and serves a tiny static web UI at ``/``.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, importer, repository, triage
from app.database import init_db
from app.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubUnavailableError,
    RateLimitError,
    RepoNotFoundError,
    get_github_client,
)
from app.ml import service as ml_service
from app.schemas import (
    BatchTriageItem,
    Category,
    CategorySource,
    ImportResult,
    Issue,
    IssueIn,
    IssueUpdate,
    ItemPage,
    Kind,
    ModelStatus,
    Priority,
    Repo,
    RepoImportRequest,
    State,
    TriageSuggestion,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ensure the database schema exists before serving requests."""
    init_db()
    yield


app = FastAPI(
    title="TriageBot API",
    version=__version__,
    description=(
        "A GitHub issue & PR triage assistant: imports items from public "
        "repositories (read-only) and suggests a category, priority, and likely "
        "owner, each with an explanation."
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness probe used by CI and by anyone bringing the app up."""
    return {"status": "ok", "version": __version__}


def _category_fields(title: str, body: str) -> dict:
    """ML category (with rule fallback) for a manually entered issue."""
    result = ml_service.classify(title, body, kind="issue")
    return {
        "category": result.category,
        "category_confidence": result.category_confidence,
        "category_source": result.category_source,
        "category_score": result.category_score,
        "category_reasons": result.category_reasons,
    }


@app.post("/triage", response_model=TriageSuggestion, tags=["triage"])
def triage_preview(payload: IssueIn) -> TriageSuggestion:
    """Triage an issue *without* saving it — the core assistant feature."""
    result = triage.triage(payload.title, payload.body)
    return TriageSuggestion(
        severity=result.severity,
        labels=result.labels,
        owner=result.owner,
        reason=result.reason,
        **_category_fields(payload.title, payload.body),
    )


@app.post("/triage/batch", response_model=list[BatchTriageItem], tags=["triage"])
def triage_batch(payload: list[IssueIn]) -> list[BatchTriageItem]:
    """Triage many incoming issues at once (e.g. a backlog import)."""
    items: list[BatchTriageItem] = []
    for issue in payload:
        result = triage.triage(issue.title, issue.body)
        items.append(
            BatchTriageItem(
                title=issue.title,
                severity=result.severity,
                labels=result.labels,
                owner=result.owner,
                reason=result.reason,
                **_category_fields(issue.title, issue.body),
            )
        )
    return items


@app.post("/issues", response_model=Issue, status_code=201, tags=["issues"])
def create_issue(payload: IssueIn) -> Issue:
    """Triage an issue and persist it."""
    result = triage.triage(payload.title, payload.body)
    issue = repository.create_issue(
        title=payload.title,
        body=payload.body,
        severity=result.severity,
        labels=result.labels,
        owner=result.owner,
        reason=result.reason,
    )
    return Issue(**issue)


@app.get("/issues", response_model=list[Issue], tags=["issues"])
def list_issues() -> list[Issue]:
    """Return all stored issues, newest first."""
    return [Issue(**row) for row in repository.list_issues()]


@app.get("/issues/{issue_id}", response_model=Issue, tags=["issues"])
def get_issue(issue_id: int) -> Issue:
    issue = repository.get_issue(issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return Issue(**issue)


@app.patch("/issues/{issue_id}", response_model=Issue, tags=["issues"])
def update_issue(issue_id: int, payload: IssueUpdate) -> Issue:
    """Update an issue. If title/body change, re-run triage automatically."""
    existing = repository.get_issue(issue_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Issue not found")

    severity = labels = owner = reason = None
    if payload.title is not None or payload.body is not None:
        new_title = payload.title if payload.title is not None else existing["title"]
        new_body = payload.body if payload.body is not None else existing["body"]
        result = triage.triage(new_title, new_body)
        severity, labels, owner, reason = (
            result.severity,
            result.labels,
            result.owner,
            result.reason,
        )

    updated = repository.update_issue(
        issue_id,
        title=payload.title,
        body=payload.body,
        severity=severity,
        labels=labels,
        owner=owner,
        reason=reason,
        status=payload.status,
    )
    return Issue(**updated)  # type: ignore[arg-type]


@app.delete("/issues/{issue_id}", status_code=204, response_class=Response, tags=["issues"])
def delete_issue(issue_id: int) -> Response:
    if not repository.delete_issue(issue_id):
        raise HTTPException(status_code=404, detail="Issue not found")
    return Response(status_code=204)


# --- GitHub import (read-only) ------------------------------------------------


def _github_http_error(exc: GitHubError) -> HTTPException:
    """Translate GitHub client errors into API responses."""
    if isinstance(exc, RepoNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RateLimitError):
        headers = {}
        retry_after = exc.retry_after
        if retry_after is None and exc.reset_at is not None:
            retry_after = max(0, int((exc.reset_at - datetime.now(UTC)).total_seconds()))
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        detail = str(exc)
        if exc.reset_at is not None:
            detail += f" Resets at {exc.reset_at.strftime('%H:%M UTC')}."
        return HTTPException(status_code=429, detail=detail, headers=headers)
    if isinstance(exc, GitHubAuthError | GitHubUnavailableError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=502, detail="GitHub request failed.")


@app.post("/repos/import", response_model=ImportResult, tags=["github"])
def import_repo(
    payload: RepoImportRequest,
    client: Annotated[GitHubClient, Depends(get_github_client)],
) -> ImportResult:
    """Fetch issues and PRs from a public GitHub repo, triage, and store them.

    Read-only: nothing is ever written back to GitHub. Re-importing a repo
    updates existing items instead of duplicating them.
    """
    owner, name = payload.repo.split("/", 1)
    try:
        summary = importer.import_repo(
            client, owner, name, state=payload.state, limit=payload.limit
        )
    except GitHubError as exc:
        raise _github_http_error(exc) from exc
    return ImportResult(
        repo=Repo(**summary.repo),
        fetched=summary.fetched,
        issues=summary.issues,
        pull_requests=summary.pull_requests,
        rate_limit_remaining=summary.rate_limit_remaining,
    )


@app.get("/repos", response_model=list[Repo], tags=["github"])
def list_repos() -> list[Repo]:
    """Repositories that have been imported."""
    return [Repo(**row) for row in repository.list_repos()]


@app.get("/items", response_model=ItemPage, tags=["github"])
def list_items(
    repo: Annotated[str | None, Query(max_length=200, description="owner/name")] = None,
    type: Annotated[Kind | None, Query(description="'issue' or 'pr'")] = None,
    category: Category | None = None,
    priority: Priority | None = None,
    state: State | None = None,
    source: Annotated[
        CategorySource | None, Query(description="Where the category came from")
    ] = None,
    q: Annotated[str | None, Query(max_length=200, description="Search title/body/#number")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ItemPage:
    """List imported issues/PRs, highest priority first, with optional filters."""
    total, rows = repository.list_items(
        filters={
            "repo": repo,
            "kind": type,
            "category": category,
            "priority": priority,
            "state": state,
            "source": source,
        },
        q=q,
        limit=limit,
        offset=offset,
    )
    return ItemPage(total=total, limit=limit, offset=offset, items=rows)


@app.get("/model", response_model=ModelStatus, tags=["ml"])
def model_info() -> ModelStatus:
    """Whether the ML category classifier is loaded, plus its recorded test scores."""
    return ModelStatus(**ml_service.model_status())


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the single-page web UI."""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
