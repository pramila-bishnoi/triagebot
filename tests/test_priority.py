"""Unit tests for the Phase 3 explainable priority scorer (app/priority.py)."""

from __future__ import annotations

import itertools
import random

import pytest

from app.priority import (
    PRIORITIES,
    label_priority,
    maintainer_priority,
    normalize_label,
    score_priority,
)
from app.triage import classify_item


def prio(title, body="", **kwargs):
    return classify_item(title, body, **kwargs)


# --- Each level -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "kwargs", "expected"),
    [
        # high: score >= 3 AND two kinds of evidence, one of them severity
        ("Critical security outage in production", {}, "high"),
        ("API returns 500 on login, users can't sign in", {}, "high"),
        ("Data loss when syncing in production", {}, "high"),
        ("App crashes on save", {"labels": ["security"]}, "high"),
        # medium
        ("Improve slow dashboard performance", {}, "medium"),
        ("Login button misaligned", {}, "medium"),
        ("", {}, "medium"),
        # low
        ("Fix typo in README", {}, "low"),
        ("Not urgent: small cosmetic issue", {}, "low"),
        ("How do I configure this?", {}, "low"),
        ("Add dark mode", {}, "low"),  # feature request, no urgency
        ("feat: add export", {"kind": "pr", "is_draft": True}, "low"),
    ],
)
def test_priority_levels(title, kwargs, expected):
    result = prio(title, **kwargs)
    assert result.priority == expected
    assert result.priority_source == "rules"
    assert result.priority_reasons[-1].startswith("total score")


def test_priorities_constant():
    assert PRIORITIES == ("low", "medium", "high")


# --- Corroboration: a keyword alone is not proof -----------------------------------


@pytest.mark.parametrize(
    "title",
    ["App crashes", "Critical outage", "Add security headers option", "Data loss when syncing"],
)
def test_single_kind_of_evidence_is_never_high(title):
    # Some of these reach score >= 3 and are capped; others stay below 3.
    result = prio(title)
    assert result.priority == "medium"
    assert not result.priority_reasons[-1].endswith("-> high")


def test_cap_explains_what_was_missing():
    reasons = prio("App crashes").priority_reasons
    assert "high needs two kinds of evidence" in reasons[-1]
    assert "found: impact" in reasons[-1]


def test_high_explains_its_evidence():
    last = prio("Critical security outage in production").priority_reasons[-1]
    assert "kinds of evidence (severe, scope" in last and last.endswith("-> high")


# --- Engagement is bounded ---------------------------------------------------------


def test_popularity_alone_never_gives_high():
    result = prio("Add dark mode", reactions=500, comments=300)
    assert result.priority == "medium"
    assert any("500 reactions (+1" in r for r in result.priority_reasons)


def test_engagement_plus_scope_without_severity_is_not_high():
    assert prio("Add dark mode for everyone", reactions=50).priority == "medium"


def test_engagement_can_corroborate_severity():
    result = prio("Data loss when syncing", reactions=12)
    assert result.priority == "high"
    assert "engagement" in result.priority_reasons[-1]


def test_comments_are_not_scored():
    assert prio("App crashes on save", comments=500).priority_reasons == (
        prio("App crashes on save").priority_reasons
    )


# --- Maintainer labels: human decisions --------------------------------------------


@pytest.mark.parametrize(
    ("label", "level"),
    [
        ("P0", "high"),
        ("p1", "high"),
        ("P2", "medium"),
        ("p3", "low"),
        ("P4", "low"),
        ("priority: high", "high"),
        ("Priority/Low", "low"),
        ("priority: medium", "medium"),
        ("Priority-1", "high"),
        ("Priority-3", "low"),
        ("priority: 0 crash", "high"),
        ("priority: 1 high", "high"),
        ("priority: 2 medium", "medium"),
        ("priority: 3 low", "low"),
        ("severity: critical", "high"),
        ("sev1", "high"),
        ("Sev-2", "medium"),
        ("high priority", "high"),
        ("Low Priority", "low"),
        ("critical", "high"),
        ("urgent", "high"),
        ("blocker", "high"),
    ],
)
def test_priority_label_formats(label, level):
    assert label_priority(label) == level


@pytest.mark.parametrize(
    "label",
    [
        "minor",
        "major",
        "high",
        "priority",
        "priority: triage",
        "good first issue",
        "security",
        "Severity-Crash",
        "status: critical",
        "A11ySev2",
        "bug",
        "",
    ],
)
def test_labels_that_do_not_set_priority(label):
    # "minor"/"major" are often semver labels; bare "high" is ambiguous.
    assert label_priority(label) is None


def test_maintainer_label_is_kept_and_rules_shown_for_reference():
    result = prio("Typo in README", labels=["P1"])
    assert result.priority == "high"
    assert result.priority_source == "maintainer_label"
    assert result.priority_reasons[0].startswith("maintainer priority label 'P1' -> high")
    assert "rules alone would suggest low" in result.priority_reasons[1]


def test_maintainer_label_overrides_strong_rule_signals():
    result = prio("Data loss in production for all users", labels=["priority: low"])
    assert result.priority == "low"
    assert result.priority_source == "maintainer_label"
    assert "rules alone would suggest high" in result.priority_reasons[1]


def test_conflicting_priority_labels_are_ignored_safely():
    result = prio("App crashes", labels=["P1", "P3"])
    assert result.priority_source == "rules"
    assert result.priority_reasons[0] == "conflicting maintainer priority labels 'P1', 'P3' ignored"
    assert result.priority == "medium"


def test_duplicate_labels_at_same_level_are_not_a_conflict():
    level, matched, conflict = maintainer_priority(["P1", "priority: high"])
    assert (level, conflict) == ("high", None)
    assert matched == ["P1", "priority: high"]


def test_normalize_label():
    assert normalize_label("Priority: 1 High") == "priority 1 high"
    assert normalize_label("kind/bug") == "kind bug"


# --- Other label signals -------------------------------------------------------------


def test_severity_topic_label_is_evidence_not_a_decision():
    result = prio("Login button misaligned", labels=["security"])
    assert result.priority == "medium"  # one kind of evidence only
    assert any("severity topic, +2" in r for r in result.priority_reasons)


def test_bug_label_is_not_severity_evidence():
    # A "bug" label describes the kind of item, not its severity (category and
    # priority are kept separate), so it cannot corroborate a "high".
    result = prio("App crashes on save", labels=["bug"])
    assert result.priority == "medium"
    assert result.priority_reasons[-1].endswith("-> medium (capped)")


def test_low_signal_labels():
    assert prio("Add CSV export", labels=["good first issue"]).priority == "low"
    assert any(
        "'wontfix' (-2)" in r for r in prio("Add CSV export", labels=["wontfix"]).priority_reasons
    )


# --- Ambiguous and misleading text ---------------------------------------------------


def test_conflicting_wording_is_flagged():
    result = prio("Critical typo in the error docs")
    assert result.priority == "medium"
    assert any("conflicting signals" in r for r in result.priority_reasons)


@pytest.mark.parametrize(
    ("title", "absent"),
    [
        ("Wrap code blocks in the docs", "scope"),  # "blocks" is not a scope word
        ("Production build docs are outdated", "scope"),  # "production" alone is not
        ("This is not a crash, just slow", "impact"),  # negated
    ],
)
def test_misleading_matches_do_not_score(title, absent):
    assert not any(f"({absent}," in r for r in prio(title).priority_reasons)


def test_negation_is_reported():
    assert any(
        "ignored negated 'crash'" in r
        for r in prio("This is not a crash, just slow").priority_reasons
    )


def test_body_counts_less_than_title():
    in_title = prio("Security problem with sessions").priority_reasons
    in_body = prio("Sessions problem", "Possibly a security problem").priority_reasons
    assert "title mentions 'security' (severe, +3)" in in_title
    assert "body mentions 'security' (severe, +2)" in in_body


def test_low_confidence_category_does_not_push_unknown_items_to_low():
    result = prio("Login button misaligned")
    assert result.category_confidence == "low"
    assert result.priority == "medium"
    assert any("low-confidence guess: no adjustment" in r for r in result.priority_reasons)


# --- Missing / incomplete metadata ----------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"labels": None},
        {"labels": [None, ""]},
        {"reactions": None},
        {"reactions": "lots"},
        {"reactions": -5},
        {"category": None},
        {"category": "unknown-category"},
    ],
)
def test_missing_or_invalid_metadata_is_neutral(kwargs):
    result = score_priority("something happened", "", **kwargs)
    assert result.priority == "medium"
    assert result.source == "rules"


def test_empty_input():
    result = score_priority("", "")
    assert (result.priority, result.score, result.evidence) == ("medium", 0, ())


# --- Determinism ----------------------------------------------------------------------


def test_same_input_same_output():
    args = ("App crashes on save for all users", "Traceback: error")
    kwargs = {"labels": ["bug", "security"], "reactions": 15}
    first = prio(*args, **kwargs)
    for _ in range(5):
        assert prio(*args, **kwargs) == first


def test_label_order_does_not_change_the_decision():
    labels = ["bug", "security", "good first issue", "P2"]
    results = {
        (r.priority, r.priority_source)
        for r in (
            prio("App crashes", labels=list(order)) for order in itertools.permutations(labels)
        )
    }
    assert results == {("medium", "maintainer_label")}


def test_outputs_always_valid():
    rng = random.Random(0)
    words = [
        "crash",
        "typo",
        "security",
        "everyone",
        "not",
        "add",
        "docs",
        "how do i",
        "in production",
        "error",
        "data loss",
        "minor",
    ]
    for _ in range(200):
        title = " ".join(rng.choice(words) for _ in range(rng.randint(0, 6)))
        result = prio(title, reactions=rng.randint(0, 30))
        assert result.priority in PRIORITIES
        assert result.priority_source in ("rules", "maintainer_label")
        assert result.priority_reasons


def test_body_only_words_add_points_but_are_not_evidence():
    # Long bodies quote templates and discussions; "security"/"error" there must not
    # combine into a "high" on their own.
    body = "Please see the security policy. I get an error when installing."
    result = prio("Drop the old license classifier", body)
    assert result.priority == "medium"
    assert any("not counted as evidence" in r for r in result.priority_reasons)
    assert result.priority_reasons[-1].endswith("(found: none) -> medium (capped)")


def test_title_evidence_with_engagement_can_be_high():
    result = prio("Certificate loading regression in 2.32.3", reactions=17)
    assert result.priority == "high"
    assert "(impact, engagement)" in result.priority_reasons[-1]


# --- Phase 3.1 changes (supported by docs/PRIORITY_EVALUATION.md, dev split) ----------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("security", True),
        ("regression", True),
        ("crash", True),
        ("data loss", True),
        ("Regression from 1.2 to 1.3", True),  # starts with the topic word
        ("kind/regression", True),
        ("Severity-Crash", True),
        ("type: security", True),
        ("issue-crash", True),
        ("sev: data loss", True),
        ("sig/security", False),  # a team label, not a severity
        ("area/crash-reporting", False),  # an area label
        ("regressionist", False),
        ("bug", False),
        ("", False),
    ],
)
def test_severity_topic_labels(label, expected):
    from app.priority import is_severity_topic_label

    assert is_severity_topic_label(label) is expected


def test_namespaced_severity_label_corroborates_title_evidence():
    result = prio("Terminal crashes when resizing", labels=["Severity-Crash"])
    assert result.priority == "high"
    assert "(impact, severity label)" in result.priority_reasons[-1]


def test_team_label_is_not_severity_evidence():
    result = prio("Terminal crashes when resizing", labels=["sig/security"])
    assert result.priority == "medium"


def test_body_only_impact_and_scope_words_are_reported_but_not_scored():
    result = prio("Settings page layout", "It fails for everyone, error in the logs")
    reasons = result.priority_reasons
    assert any("(impact; body-only, not scored)" in r for r in reasons)
    assert any("(scope; body-only, not scored)" in r for r in reasons)
    assert result.priority == "medium"


def test_body_only_severe_words_still_score_but_are_not_evidence():
    reasons = prio("Sessions problem", "Possibly a security problem").priority_reasons
    assert "body mentions 'security' (severe, +2)" in reasons
    assert any("not counted as evidence" in r for r in reasons)


def test_feature_requests_lean_low_but_can_be_raised():
    plain = prio("Add an option to export results")
    assert plain.category == "feature" and plain.priority == "low"
    assert "category 'feature' (-1)" in plain.priority_reasons
    # A maintainer label still decides; strong evidence still counts.
    assert prio("Add an option to export results", labels=["P1"]).priority == "high"


def test_low_confidence_feature_guess_is_not_penalised():
    # PRs without signals default to 'feature' with low confidence: no adjustment.
    result = prio("Refactor settings loader", kind="pr")
    assert result.category_confidence == "low"
    assert result.priority == "medium"
