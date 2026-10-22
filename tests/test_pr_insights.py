"""Unit tests for the PR insight rules (app/pr_insights.py)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.pr_insights import (
    Thresholds,
    analyze,
    days_between,
    parse_time,
    size_label,
    summarize_reviews,
    thresholds_from_env,
)

NOW = datetime(2026, 10, 2, tzinfo=UTC)


def pr(**overrides):
    base = {
        "state": "open",
        "is_draft": False,
        "created_at": "2026-09-22T00:00:00Z",
        "updated_at": "2026-09-30T00:00:00Z",
        "details_fetched_at": "2026-10-01 00:00:00",
    }
    return {**base, **overrides}


def codes(result):
    return [i.code for i in result.insights]


# --- Parsing and arithmetic -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-01T12:00:00Z", datetime(2026, 9, 1, 12, tzinfo=UTC)),
        ("2026-09-01 12:00:00", datetime(2026, 9, 1, 12, tzinfo=UTC)),  # SQLite format
        ("", None),
        (None, None),
        ("not a date", None),
    ],
)
def test_parse_time(value, expected):
    assert parse_time(value) == expected


def test_days_between_never_negative():
    assert days_between(datetime(2026, 10, 5, tzinfo=UTC), NOW) == 0.0
    assert days_between(None, NOW) is None


@pytest.mark.parametrize(
    ("lines", "label"),
    [
        (None, None),
        (0, "XS"),
        (9, "XS"),
        (10, "S"),
        (99, "S"),
        (100, "M"),
        (499, "M"),
        (500, "L"),
        (999, "L"),
        (1000, "XL"),
        (25000, "XL"),
    ],
)
def test_size_label_boundaries(lines, label):
    assert size_label(lines) == label


# --- Status -------------------------------------------------------------------------


def test_open_pr_has_age_and_activity():
    result = analyze(pr(), now=NOW)
    assert result.status == "open"
    assert (result.age_days, result.idle_days) == (10.0, 2.0)
    assert result.flags == []


def test_draft():
    result = analyze(pr(is_draft=True), now=NOW)
    assert result.status == "draft" and "draft" in result.flags
    assert next(i for i in result.insights if i.code == "draft").source == "github"


def test_merged_vs_closed():
    merged = analyze(pr(state="closed", merged_at="2026-09-25T00:00:00Z"), now=NOW)
    closed = analyze(pr(state="closed", merged_at=None), now=NOW)
    assert merged.status == "merged" and "merged on 2026-09-25" in merged.insights[0].reason
    assert closed.status == "closed" and closed.insights[0].title == "Closed without merging"


def test_closed_draft_is_not_a_draft():
    assert analyze(pr(state="closed", is_draft=True), now=NOW).status == "closed"


# --- Stale ----------------------------------------------------------------------------


def test_stale_at_threshold():
    stale = analyze(pr(updated_at="2026-09-02T00:00:00Z"), now=NOW)  # 30 days
    fresh = analyze(pr(updated_at="2026-09-03T00:00:00Z"), now=NOW)  # 29 days
    assert "stale" in stale.flags and "stale" not in fresh.flags
    reason = next(i for i in stale.insights if i.code == "stale")
    assert reason.source == "calculated" and "threshold: 30 days" in reason.reason


def test_closed_prs_are_never_stale():
    old = analyze(pr(state="closed", updated_at="2025-01-01T00:00:00Z"), now=NOW)
    assert "stale" not in old.flags


def test_stale_threshold_is_configurable():
    result = analyze(
        pr(updated_at="2026-09-25T00:00:00Z"), now=NOW, thresholds=Thresholds(stale_days=7)
    )
    assert "stale" in result.flags


# --- Reviews --------------------------------------------------------------------------


def test_awaiting_review_from_pending_requests():
    result = analyze(pr(requested_reviewers=2, requested_teams=1), now=NOW)
    assert "awaiting_review" in result.flags
    reason = next(i for i in result.insights if i.code == "awaiting_review").reason
    assert "2 reviewers, 1 team" in reason


@pytest.mark.parametrize(
    "override",
    [
        {"is_draft": True},
        {"state": "closed"},
        {"requested_reviewers": 0, "requested_teams": 0},
        {"requested_reviewers": None, "requested_teams": None},
    ],
)
def test_not_awaiting_review(override):
    base = {"requested_reviewers": 1, "requested_teams": 0, **override}
    assert "awaiting_review" not in analyze(pr(**base), now=NOW).flags


def test_summarize_reviews_uses_each_reviewers_latest_decision():
    reviews = [
        {"user": {"login": "a"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-09-01"},
        {"user": {"login": "a"}, "state": "APPROVED", "submitted_at": "2026-09-02"},
        {"user": {"login": "b"}, "state": "APPROVED", "submitted_at": "2026-09-03"},
        {"user": {"login": "b"}, "state": "COMMENTED", "submitted_at": "2026-09-04"},  # no change
        {"user": {"login": "c"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-09-01"},
        {"user": {"login": "c"}, "state": "DISMISSED", "submitted_at": "2026-09-05"},  # cleared
        {"user": {"login": "d"}, "state": "PENDING", "submitted_at": None},  # ignored
    ]
    assert summarize_reviews(reviews) == {
        "reviews_total": 6,
        "reviews_commented": 1,
        "reviews_approved": 2,
        "reviews_changes_requested": 0,
    }


def test_summarize_reviews_orders_by_time_not_input_order():
    reviews = [
        {"user": {"login": "a"}, "state": "APPROVED", "submitted_at": "2026-09-02"},
        {"user": {"login": "a"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-09-01"},
    ]
    assert summarize_reviews(reviews)["reviews_approved"] == 1


def test_changes_requested_beats_approved():
    result = analyze(pr(reviews_total=2, reviews_approved=1, reviews_changes_requested=1), now=NOW)
    assert "changes_requested" in result.flags and "approved" not in result.flags


def test_approved_is_not_merge_readiness():
    result = analyze(pr(reviews_total=1, reviews_approved=1, reviews_changes_requested=0), now=NOW)
    reason = next(i for i in result.insights if i.code == "approved").reason
    assert "does not mean the PR can be merged" in reason


def test_no_reviews_only_when_review_data_loaded():
    assert "no_reviews" in analyze(pr(reviews_total=0), now=NOW).flags
    assert "no_reviews" not in analyze(pr(reviews_total=None), now=NOW).flags
    assert "no_reviews" not in analyze(pr(reviews_total=0, is_draft=True), now=NOW).flags


# --- Size -----------------------------------------------------------------------------


def test_size_and_large_by_lines():
    result = analyze(pr(additions=450, deletions=60, changed_files=3), now=NOW)
    assert (result.lines_changed, result.size) == (510, "L")
    assert "large" in result.flags
    large = next(i for i in result.insights if i.code == "large")
    assert "510 lines changed ≥ 500" in large.reason and "code quality" in large.reason


def test_large_by_files_only():
    result = analyze(pr(additions=50, deletions=10, changed_files=25), now=NOW)
    assert result.size == "S" and "large" in result.flags
    assert "25 files changed ≥ 20" in next(i for i in result.insights if i.code == "large").reason


def test_large_thresholds_are_configurable():
    result = analyze(
        pr(additions=80, deletions=0, changed_files=1),
        now=NOW,
        thresholds=Thresholds(large_lines=50),
    )
    assert "large" in result.flags


def test_missing_metadata_is_unknown_not_zero():
    result = analyze({"state": "open"}, now=NOW)
    assert result.status == "open"
    assert (result.age_days, result.idle_days, result.size, result.lines_changed) == (
        None,
        None,
        None,
        None,
    )
    assert result.flags == []
    assert codes(result) == ["details_missing"]


def test_partial_size_data_is_not_guessed():
    result = analyze(pr(additions=10, deletions=None), now=NOW)
    assert result.size is None and "large" not in result.flags


def test_every_insight_has_a_reason_and_source():
    result = analyze(
        pr(
            is_draft=False,
            requested_reviewers=1,
            reviews_total=1,
            reviews_approved=1,
            additions=900,
            deletions=200,
            changed_files=30,
            updated_at="2026-08-01T00:00:00Z",
        ),
        now=NOW,
    )
    assert result.insights
    for insight in result.insights:
        assert insight.reason and insight.source in ("github", "calculated")


def test_deterministic():
    data = pr(requested_reviewers=1, additions=600, deletions=0, changed_files=2)
    assert analyze(data, now=NOW) == analyze(dict(data), now=NOW)


# --- Configuration --------------------------------------------------------------------


def test_thresholds_from_env(monkeypatch):
    monkeypatch.setenv("TRIAGEBOT_PR_STALE_DAYS", "14")
    monkeypatch.setenv("TRIAGEBOT_PR_LARGE_LINES", "-5")  # invalid -> default
    monkeypatch.setenv("TRIAGEBOT_PR_LARGE_FILES", "many")  # invalid -> default
    assert thresholds_from_env() == Thresholds(stale_days=14, large_lines=500, large_files=20)


def test_closed_with_unknown_merge_state_is_not_called_unmerged():
    result = analyze(pr(state="closed", merged_at=None, merge_known=False), now=NOW)
    assert result.status == "closed"
    assert result.insights[0].code == "closed_unknown"
    assert "re-import" in result.insights[0].reason
