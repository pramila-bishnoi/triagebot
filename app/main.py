"""FastAPI application entry point for TriageBot.

Wires together the persistence layer, the triage engine, the read-only GitHub
importer, and the HTTP API, and serves a tiny static web UI at ``/``.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, duplicates, importer, pr_insights, repository, triage
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
    DuplicateAnalysis,
    DuplicateDecision,
    DuplicateRecord,
    DuplicateRefreshRequest,
    DuplicateSuggestion,
    ImportResult,
    Issue,
    IssueIn,
    IssueUpdate,
    ItemDuplicates,
    ItemPage,
    Kind,
    ModelStatus,
    PRDetailsRequest,
    PRDetailsResult,
    PRFlag,
    Priority,
    PRPage,
    PRSize,
    PRStatus,
    PRSummary,
    PRThresholds,
    PRView,
    Repo,
    RepoImportRequest,
    RepoSyncRequest,
    State,
    SyncResult,
    TriageSuggestion,
)

log = logging.getLogger(__name__)

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

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def json_only_writes_and_security_headers(request: Request, call_next):
    """Reject non-JSON write bodies; add basic browser security headers.

    A web page on another site can send a "simple" cross-origin POST (a form, or
    ``fetch`` with ``mode: "no-cors"``) whose body has no JSON content type. Such
    requests skip the browser's CORS preflight, and FastAPI parses a body with no
    content type as JSON — so without this check any site a user visits could
    create issues or start imports against a TriageBot running on their machine.
    Requiring ``application/json`` forces a preflight, which fails because the
    app sends no CORS headers. This is **not** authentication: anyone who can
    reach the server directly can still call every endpoint.
    """
    has_body = (
        request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers
    )
    if request.method in _UNSAFE_METHODS and has_body:
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return JSONResponse(
                status_code=415, content={"detail": "Request body must be application/json."}
            )
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness probe used by CI and by anyone bringing the app up."""
    return {"status": "ok", "version": __version__}


def _category_fields(title: str, body: str) -> dict:
    """ML category (with rule fallback) and suggested priority for a manual issue."""
    result = ml_service.classify(title, body, kind="issue")
    return {
        "category": result.category,
        "category_confidence": result.category_confidence,
        "category_source": result.category_source,
        "category_score": result.category_score,
        "category_reasons": result.category_reasons,
        "priority": result.priority,
        "priority_source": result.priority_source,
        "priority_reasons": result.priority_reasons,
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


MAX_BATCH = 100  # bounds the work one request can trigger (each item runs the ML model)


@app.post("/triage/batch", response_model=list[BatchTriageItem], tags=["triage"])
def triage_batch(
    payload: Annotated[list[IssueIn], Body(max_length=MAX_BATCH)],
) -> list[BatchTriageItem]:
    """Triage up to 100 issues at once (e.g. a backlog import)."""
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
        duplicates=_refresh_duplicates(summary.repo),
        pr_metadata=summary.pr_metadata,
    )


def _refresh_duplicates(repo: dict) -> DuplicateAnalysis | None:
    """Re-run duplicate detection after new data arrived; never fails the caller.

    The analysis writes in one transaction, so a failure leaves the previous
    suggestions (and every maintainer decision) as they were.
    """
    try:
        return DuplicateAnalysis(repo=repo["full_name"], **duplicates.analyze_repo(repo["id"]))
    except Exception:
        log.exception("duplicate analysis failed for %s", repo["full_name"])
        return None


@app.post("/repos/sync", response_model=SyncResult, tags=["github"])
def sync_repo(
    payload: RepoSyncRequest,
    client: Annotated[GitHubClient, Depends(get_github_client)],
) -> SyncResult:
    """Update an imported repository with everything changed on GitHub since its last sync.

    Fetches issues and PRs in **any** state updated since the repository was
    last known to be current (so items closed or reopened on GitHub change state
    here), oldest change first, up to ``limit``. Read-only towards GitHub; a
    failed sync stores nothing.
    """
    repo = repository.get_repo_by_name(payload.repo)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository has not been imported.")
    try:
        summary = importer.sync_repo(client, repo, limit=payload.limit)
    except GitHubError as exc:
        raise _github_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SyncResult(
        repo=Repo(**summary.repo),
        since=summary.since,
        fetched=summary.fetched,
        added=summary.added,
        updated=summary.updated,
        closed=summary.closed,
        reopened=summary.reopened,
        complete=summary.complete,
        synced_through=summary.synced_through,
        rate_limit_remaining=summary.rate_limit_remaining,
        duplicates=_refresh_duplicates(summary.repo),
        pr_metadata=summary.pr_metadata,
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


# --- Duplicate detection (suggestions only; nothing is changed on GitHub) ----------


@app.post("/duplicates/refresh", response_model=DuplicateAnalysis, tags=["duplicates"])
def refresh_duplicates(payload: DuplicateRefreshRequest) -> DuplicateAnalysis:
    """Run duplicate detection for an imported repository (skipped if nothing changed)."""
    repo = repository.get_repo_by_name(payload.repo)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository has not been imported.")
    return DuplicateAnalysis(
        repo=repo["full_name"], **duplicates.analyze_repo(repo["id"], force=payload.force)
    )


@app.get("/items/{item_id}/duplicates", response_model=ItemDuplicates, tags=["duplicates"])
def item_duplicates(item_id: int) -> ItemDuplicates:
    """Possible duplicates of one imported issue, with explanations and review status."""
    item = repository.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    note = None
    if item["kind"] == "pr":
        note = "Duplicate detection compares issues only; pull requests are not compared."
    suggestions = [
        DuplicateSuggestion(**{**s, "explanation": duplicates.explain(s)})
        for s in repository.duplicates_for_item(item_id)
    ]
    return ItemDuplicates(
        item_id=item["id"],
        number=item["number"],
        title=item["title"],
        threshold=duplicates.current_threshold(),
        note=note,
        suggestions=suggestions,
    )


@app.patch("/duplicates/{suggestion_id}", response_model=DuplicateRecord, tags=["duplicates"])
def decide_duplicate(suggestion_id: int, payload: DuplicateDecision) -> DuplicateRecord:
    """Record a maintainer's review: confirmed, rejected, or back to pending."""
    record = repository.set_suggestion_status(suggestion_id, payload.status)
    if record is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return DuplicateRecord(**record)


# --- Pull-request insights (read-only) --------------------------------------------


def _pr_view(row: dict, now, thresholds) -> PRView:
    result = pr_insights.analyze(row, now=now, thresholds=thresholds)
    return PRView(
        **{k: row.get(k) for k in PRView.model_fields if k in row},
        status=result.status,
        age_days=result.age_days,
        idle_days=result.idle_days,
        lines_changed=result.lines_changed,
        size=result.size,
        flags=result.flags,
        insights=[vars(i) for i in result.insights],
    )


_PR_SORTS = {
    "updated": lambda v: v.updated_at,
    "age": lambda v: v.age_days or 0.0,
    "idle": lambda v: v.idle_days or 0.0,
    "size": lambda v: v.lines_changed if v.lines_changed is not None else -1,
}


@app.get("/prs", response_model=PRPage, tags=["pull requests"])
def list_prs(
    repo: Annotated[str | None, Query(max_length=200, description="owner/name")] = None,
    status: PRStatus | None = None,
    author: Annotated[str | None, Query(max_length=100)] = None,
    flag: PRFlag | None = None,
    size: PRSize | None = None,
    min_age_days: Annotated[float | None, Query(ge=0)] = None,
    max_age_days: Annotated[float | None, Query(ge=0)] = None,
    q: Annotated[str | None, Query(max_length=200, description="Search title/body/#number")] = None,
    sort: Annotated[str, Query(pattern="^(updated|age|idle|size)$")] = "updated",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PRPage:
    """Imported pull requests with insights (newest activity first by default).

    ``summary`` counts cover every PR matching repo / author / search, so the
    dashboard can show totals while the list is filtered further.
    """
    now, thresholds = pr_insights.utcnow(), pr_insights.thresholds_from_env()
    views = [
        _pr_view(row, now, thresholds) for row in repository.list_prs(repo=repo, author=author, q=q)
    ]
    summary = PRSummary(
        total=len(views),
        **{s: sum(v.status == s for v in views) for s in ("open", "draft", "merged", "closed")},
        **{
            f: sum(f in v.flags for v in views)
            for f in (
                "stale",
                "awaiting_review",
                "large",
                "changes_requested",
                "approved",
                "no_reviews",
            )
        },
        with_details=sum(v.details_fetched_at is not None for v in views),
    )
    if status:
        views = [v for v in views if v.status == status]
    if flag:
        views = [v for v in views if flag in v.flags]
    if size:
        views = [v for v in views if (v.size or "unknown") == size]
    if min_age_days is not None:
        views = [v for v in views if v.age_days is not None and v.age_days >= min_age_days]
    if max_age_days is not None:
        views = [v for v in views if v.age_days is not None and v.age_days <= max_age_days]
    views.sort(key=_PR_SORTS[sort], reverse=True)
    return PRPage(
        total=len(views),
        limit=limit,
        offset=offset,
        summary=summary,
        thresholds=PRThresholds(**vars(thresholds)),
        items=views[offset : offset + limit],
    )


@app.get("/prs/{item_id}", response_model=PRView, tags=["pull requests"])
def get_pr(item_id: int) -> PRView:
    row = repository.get_pr(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Pull request not found")
    return _pr_view(row, pr_insights.utcnow(), pr_insights.thresholds_from_env())


@app.post("/prs/details", response_model=PRDetailsResult, tags=["pull requests"])
def load_pr_details(
    payload: PRDetailsRequest,
    client: Annotated[GitHubClient, Depends(get_github_client)],
) -> PRDetailsResult:
    """Load size and review data for up to ``limit`` PRs (2 read-only GitHub requests each).

    Stops cleanly on a GitHub rate limit; whatever was loaded is kept.
    """
    repo = repository.get_repo_by_name(payload.repo)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository has not been imported.")
    try:
        result = importer.fetch_pr_details(
            client, repo, limit=payload.limit, only_open=payload.only_open, force=payload.force
        )
    except GitHubError as exc:
        raise _github_http_error(exc) from exc
    return PRDetailsResult(**result)


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
