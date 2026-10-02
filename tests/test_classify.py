"""Unit tests for classify_item(): category, priority, and explanations."""

from __future__ import annotations

import pytest

from app.triage import CATEGORIES, PRIORITIES, classify_item, find_keyword


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("App crashes on startup with a traceback", "bug"),
        ("Feature request: support dark mode", "feature"),
        ("Fix typo in README", "documentation"),
        ("How do I configure the proxy?", "question"),
        ("Add an example to the tutorial", "documentation"),
    ],
)
def test_category(title, category):
    assert classify_item(title).category == category


def test_github_label_outweighs_body_keywords():
    result = classify_item(
        "Dark mode", "it would be nice, no error here", labels=["documentation"]
    )
    assert result.category == "documentation"
    assert any("GitHub label 'documentation'" in r for r in result.category_reasons)


def test_conventional_commit_prefix_for_prs():
    assert classify_item("feat(api): pagination", kind="pr").category == "feature"
    assert classify_item("fix: handle empty input", kind="pr").category == "bug"
    assert classify_item("docs: clarify install", kind="pr").category == "documentation"


def test_conventional_prefix_ignored_for_issues():
    result = classify_item("feat: something", kind="issue")
    assert not any("prefix" in r for r in result.category_reasons)


def test_default_category_is_low_confidence():
    issue = classify_item("Make the input field bigger")
    assert issue.category == "question"
    assert issue.category_confidence == "low"
    assert any("no category signals" in r for r in issue.category_reasons)
    assert classify_item("Bump version", kind="pr").category == "feature"


def test_negated_category_keyword_is_ignored():
    result = classify_item("This is not a bug, how do I enable caching?")
    assert result.category == "question"
    assert any("ignored negated 'bug'" in r for r in result.category_reasons)


def test_html_comments_from_templates_are_ignored():
    body = "<!-- Please report security vulnerabilities privately; crashes go here -->\nAdd X"
    result = classify_item("Small request", body)
    assert result.priority != "high"
    assert not any("security" in r for r in result.priority_reasons)


@pytest.mark.parametrize(
    ("title", "priority"),
    [
        ("Critical security outage in production", "high"),
        ("API returns 500 on login, users can't sign in", "high"),
        ("Improve slow dashboard performance", "medium"),
        ("Fix typo in README documentation", "low"),
        ("Not urgent: small cosmetic issue", "low"),
        ("How do I configure this?", "low"),
    ],
)
def test_priority(title, priority):
    assert classify_item(title).priority == priority


def test_not_urgent_regression():
    result = classify_item("Not urgent: small cosmetic issue")
    assert result.priority == "low"
    assert any("ignored negated 'urgent'" in r for r in result.priority_reasons)


def test_body_match_counts_less_than_title_match():
    in_title = classify_item("Security problem with sessions")
    in_body = classify_item("Sessions problem", "Possibly a security problem")
    assert any("title mentions 'security' (severe, +3)" in r for r in in_title.priority_reasons)
    assert any("body mentions 'security' (severe, +2)" in r for r in in_body.priority_reasons)


def test_engagement_and_labels_raise_priority():
    quiet = classify_item("Add CSV export")
    popular = classify_item("Add CSV export", reactions=25, comments=12)
    labelled = classify_item("Add CSV export", labels=["priority: high"])
    assert quiet.priority == "medium"
    assert popular.priority == "high"
    assert labelled.priority == "high"
    assert any("25 reactions (+2)" in r for r in popular.priority_reasons)


def test_low_priority_label():
    assert classify_item("Add CSV export", labels=["good first issue"]).priority == "low"


def test_priority_explanation_ends_with_total():
    reasons = classify_item("App crashes").priority_reasons
    assert reasons[-1].startswith("total score +3")
    assert reasons[-1].endswith("-> high")


def test_owner_hint_is_optional():
    assert classify_item("API endpoint returns wrong data").owner_hint == "backend-team"
    assert classify_item("Something odd happens").owner_hint is None


@pytest.mark.parametrize("title", ["", "x", "???", "URGENT!!!", "not not not"])
def test_outputs_always_valid(title):
    result = classify_item(title, kind="issue")
    assert result.category in CATEGORIES
    assert result.priority in PRIORITIES
    assert result.category_confidence in ("low", "medium", "high")


def test_find_keyword_whole_words():
    assert find_keyword("the author", ("auth",)).keyword is None
    assert find_keyword("auth token expired", ("auth",)).keyword == "auth"
    assert find_keyword("took 1500ms", ("500",)).keyword is None
    assert find_keyword("http 500.", ("500",)).keyword == "500"
    assert find_keyword("non-urgent tweak", ("urgent",)).negated == ("urgent",)


def test_long_body_cannot_outvote_title():
    body = "error exception crash traceback broken regression fail wrong panic"
    result = classify_item("Clarify the install docs", body)
    assert result.category == "documentation"
    assert sum("(+1 bug)" in r for r in result.category_reasons) == 2  # body capped


def test_tie_prefers_title_evidence():
    # bug: body only (2), documentation: title only (2)
    result = classify_item("Update README", "error and crash")
    assert result.category == "documentation"
    assert result.category_confidence == "low"


def test_dependency_bump_ignores_release_notes():
    notes = "Release notes: critical security fix for crash, error handling, docs"
    result = classify_item("Bump actions/setup-python from 5.6.0 to 6.0.0", notes, kind="pr")
    assert result.priority == "medium"
    assert "body (upstream release notes) ignored" in result.category_reasons[0]
    assert not any("security" in r for r in result.priority_reasons)
    # Only PRs get this treatment.
    issue = classify_item("Bump foo from 1 to 2", notes, kind="issue")
    assert any("body mentions" in r for r in issue.category_reasons)
