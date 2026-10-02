"""Explainable, rule-based issue-triage engine.

Given the free-text ``title`` and ``body`` of an issue or pull request, this
module provides two entry points:

* :func:`triage` — the original manual-triage engine. Suggests a ``severity``
  (low / medium / high / urgent), one or more ``labels`` and a likely
  ``owner``. Used by ``/triage`` and ``/issues``.
* :func:`classify_item` — used for items imported from GitHub. Suggests a
  ``category`` (bug / feature / documentation / question), a ``priority``
  (low / medium / high) and an optional ``owner_hint``, with a list of
  human-readable reasons for every point that was added or subtracted.

It is intentionally rule-based rather than LLM-backed so the tool runs with
**no API keys or network access** — which keeps it fully demoable and testable
in CI. The rule tables below are the single place to tune behaviour; adding a
keyword is a one-line change with an accompanying test.

Keywords match whole words only (``auth`` does not match "author", ``500`` does
not match "1500ms"), allowing simple inflections (``crash`` matches "crashes",
"crashed", "crashing"). A keyword directly preceded by a negation such as
"not", "no", "never" or "non-" (optionally with a filler word, as in "not a
bug" or "not very urgent") is ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

Table = list[tuple[str, tuple[str, ...]]]

# --- Severity (legacy triage) ---------------------------------------------
# Ordered most-severe first; the first matching bucket wins so "urgent"
# always beats "high", etc.
SEVERITY_KEYWORDS: Table = [
    ("urgent", ("urgent", "asap", "immediately", "critical", "outage", "data loss",
                "security", "vulnerability", "cve", "production down", "p0")),
    ("high", ("crash", "error", "exception", "fail", "broken",
              "blocker", "regression", "cannot", "can't", "500", "deadline")),
    ("medium", ("bug", "slow", "performance", "improve", "refactor",
                "unexpected", "incorrect")),
    ("low", ("typo", "nit", "minor", "someday", "nice to have", "cosmetic", "cleanup")),
]

# --- Labels (legacy triage) -----------------------------------------------
# All matching labels are attached (an issue can be both "bug" and "docs").
LABEL_KEYWORDS: Table = [
    ("bug", ("bug", "crash", "error", "exception", "fail", "broken",
             "regression", "500", "stack trace", "traceback")),
    ("documentation", ("doc", "documentation", "readme", "typo", "comment",
                       "example", "tutorial")),
    ("enhancement", ("add", "feature", "implement", "support", "request", "would be nice",
                     "new", "enhancement")),
    ("performance", ("slow", "performance", "latency", "timeout", "memory", "cpu")),
    ("security", ("security", "vulnerability", "cve", "xss", "sql injection", "csrf",
                  "auth", "token leak")),
    ("chore", ("refactor", "cleanup", "upgrade", "bump", "dependency", "chore", "ci",
               "build")),
]

# --- Ownership routing ----------------------------------------------------
# Keyword -> owning team. First match wins; falls back to a per-label default,
# then to a global default. Mimics a lightweight CODEOWNERS-style router.
OWNER_KEYWORDS: Table = [
    ("frontend-team", ("ui", "css", "html", "button", "page", "layout", "react",
                       "frontend", "browser", "responsive")),
    ("backend-team", ("api", "server", "endpoint", "database", "db", "query", "500",
                      "backend", "migration")),
    ("security-team", ("security", "vulnerability", "cve", "xss", "csrf", "auth",
                       "token", "injection")),
    ("infra-team", ("ci", "pipeline", "deploy", "docker", "kubernetes", "infra",
                    "build", "workflow")),
    ("docs-team", ("doc", "documentation", "readme", "tutorial")),
]

LABEL_DEFAULT_OWNER = {
    "bug": "backend-team",
    "documentation": "docs-team",
    "enhancement": "product-team",
    "performance": "backend-team",
    "security": "security-team",
    "chore": "infra-team",
}

DEFAULT_SEVERITY = "medium"
DEFAULT_LABEL = "triage"
DEFAULT_OWNER = "maintainers"

VALID_SEVERITIES = ("low", "medium", "high", "urgent")

# --- Category (imported items) --------------------------------------------
CATEGORIES = ("bug", "feature", "documentation", "question")

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "bug": ("bug", "crash", "error", "exception", "fail", "failure", "broken", "regression",
            "traceback", "stack trace", "segfault", "panic", "500", "incorrect",
            "wrong", "unexpected", "not working", "doesn't work", "fix", "outage",
            "slow", "leak", "hang", "freeze"),
    "feature": ("feature", "feature request", "enhancement", "add", "implement",
                "support", "proposal", "propose", "would be nice", "allow", "option",
                "improve", "request"),
    "documentation": ("doc", "documentation", "readme", "typo", "tutorial", "example",
                      "guide", "docstring", "changelog", "wiki", "spelling", "wording"),
    "question": ("question", "how do i", "how to", "how can i", "is it possible",
                 "is there a way", "clarification", "what is the", "confused", "wondering"),
}

# GitHub label names that map directly to a category.
CATEGORY_LABELS: dict[str, tuple[str, ...]] = {
    "bug": ("bug", "type: bug", "kind/bug", "defect"),
    "feature": ("enhancement", "feature", "feature request", "type: feature",
                "kind/feature"),
    "documentation": ("documentation", "docs", "type: docs", "kind/documentation"),
    "question": ("question", "type: question", "kind/question", "support"),
}

# Conventional-commit style PR titles, e.g. "fix(api): ..." or "docs: ...".
CONVENTIONAL_PREFIX = re.compile(r"^\s*(feat|fix|docs?)(\([^)]*\))?!?:", re.IGNORECASE)
CONVENTIONAL_CATEGORY = {"feat": "feature", "fix": "bug", "doc": "documentation",
                         "docs": "documentation"}

TITLE_WEIGHT = 2
BODY_WEIGHT = 1
BODY_CAP = 2       # max points the body can add per category, so long bodies don't drown the title
LABEL_WEIGHT = 3

# Automated dependency updates ("Bump foo from 1.2 to 1.3"): their bodies are
# upstream release notes, which would otherwise look like a pile of bug reports.
DEPENDENCY_BUMP = re.compile(r"^\s*(?:build\(deps[^)]*\):\s*)?bump\s+\S+.*\bfrom\s+\S+\s+to\s+\S+",
                             re.IGNORECASE)

# --- Priority (imported items) --------------------------------------------
PRIORITIES = ("low", "medium", "high")

# (name, points, keywords). Each group contributes at most once. A match found
# only in the body counts one point less (towards zero) than a title match,
# because issue templates often mention words like "security" or "crash".
PRIORITY_GROUPS: list[tuple[str, int, tuple[str, ...]]] = [
    ("severe", 3, ("urgent", "critical", "outage", "data loss", "security",
                   "vulnerability", "cve", "p0", "blocker", "production down",
                   "asap")),
    ("impact", 2, ("crash", "regression", "broken", "fail", "failure", "error", "exception",
                   "cannot", "can't", "500", "p1", "data corruption")),
    ("minor", -2, ("typo", "nit", "minor", "cosmetic", "someday", "nice to have",
                   "cleanup", "low priority", "trivial", "spelling", "wording")),
]

PRIORITY_HIGH_LABELS = ("critical", "urgent", "p0", "p1", "blocker", "security",
                        "high priority", "priority: high", "priority/high")
PRIORITY_LOW_LABELS = ("low priority", "priority: low", "priority/low", "p3", "p4",
                       "good first issue", "wontfix")

CATEGORY_PRIORITY_ADJUST = {"bug": 1, "feature": 0, "documentation": -1, "question": -1}

HIGH_THRESHOLD = 3   # score >= 3  -> high
LOW_THRESHOLD = -1   # score <= -1 -> low


# --- Matching helpers -------------------------------------------------------

_NEGATION = re.compile(
    r"(?:^|[^\w'])(?:not|no|never|without|non|isn't|aren't|wasn't|isnt)[\s-]+"
    r"(?:(?:a|an|the|really|very|so|too|that|yet|actually)\s+)?$"
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@lru_cache(maxsize=1024)
def _pattern(keyword: str) -> re.Pattern[str]:
    """Whole-word pattern that tolerates simple English inflections."""
    return re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?:s|es|ed|d|ing)?(?!\w)")


def _normalize(text: str) -> str:
    return text.lower().replace("’", "'")


@dataclass(frozen=True)
class Hit:
    """Result of searching text for a list of keywords."""

    keyword: str | None = None          # first non-negated keyword found
    negated: tuple[str, ...] = ()       # keywords seen but cancelled by a negation


def find_keyword(text: str, keywords: tuple[str, ...]) -> Hit:
    """Return the first keyword (in table order) that occurs un-negated in ``text``.

    ``text`` must already be normalised (lower-case).
    """
    negated: list[str] = []
    for keyword in keywords:
        for match in _pattern(keyword).finditer(text):
            preceding = text[max(0, match.start() - 40):match.start()]
            if _NEGATION.search(preceding):
                if keyword not in negated:
                    negated.append(keyword)
                continue
            return Hit(keyword, tuple(negated))
    return Hit(None, tuple(negated))


def _negation_note(negated: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f"'{kw}'" for kw in negated)


# --- Legacy manual triage ---------------------------------------------------


@dataclass(frozen=True)
class TriageResult:
    """The outcome of triaging a single issue."""

    severity: str
    labels: list[str] = field(default_factory=list)
    owner: str = DEFAULT_OWNER
    reason: str = ""


def _first_match(text: str, table: Table) -> tuple[tuple[str, str] | None, list[str]]:
    """Return ``((label, keyword) | None, negated_keywords)`` for the first bucket that matches."""
    negated: list[str] = []
    for label, keywords in table:
        hit = find_keyword(text, keywords)
        negated.extend(kw for kw in hit.negated if kw not in negated)
        if hit.keyword:
            return (label, hit.keyword), negated
    return None, negated


def _all_matches(text: str, table: Table) -> list[tuple[str, str]]:
    """Return every bucket that matches, preserving table order."""
    matches: list[tuple[str, str]] = []
    for label, keywords in table:
        hit = find_keyword(text, keywords)
        if hit.keyword:
            matches.append((label, hit.keyword))
    return matches


def triage(title: str, body: str | None = None) -> TriageResult:
    """Triage a single incoming issue.

    Args:
        title: The issue title (required).
        body: Optional issue body / description.

    Returns:
        A :class:`TriageResult` with severity, labels, likely owner, and a
        short human-readable reason explaining each choice.
    """
    text = _normalize(f"{title} {body or ''}")

    severity_match, negated = _first_match(text, SEVERITY_KEYWORDS)
    severity = severity_match[0] if severity_match else DEFAULT_SEVERITY

    label_matches = _all_matches(text, LABEL_KEYWORDS)
    labels = [label for label, _ in label_matches] or [DEFAULT_LABEL]

    owner_match, _ = _first_match(text, OWNER_KEYWORDS)
    if owner_match:
        owner = owner_match[0]
        owner_reason = f"owner={owner} (matched '{owner_match[1]}')"
    else:
        owner = LABEL_DEFAULT_OWNER.get(labels[0], DEFAULT_OWNER)
        owner_reason = f"owner={owner} (routed by label '{labels[0]}')"

    reasons: list[str] = []
    if severity_match:
        reasons.append(f"severity={severity} (matched '{severity_match[1]}')")
    else:
        reasons.append(f"severity={severity} (default, no keyword matched)")
    if negated:
        reasons.append(f"ignored negated {_negation_note(negated)}")
    if label_matches:
        joined = ", ".join(f"{lbl} ('{kw}')" for lbl, kw in label_matches)
        reasons.append(f"labels: {joined}")
    else:
        reasons.append(f"labels: {DEFAULT_LABEL} (default, no keyword matched)")
    reasons.append(owner_reason)

    return TriageResult(
        severity=severity,
        labels=labels,
        owner=owner,
        reason="; ".join(reasons),
    )


# --- Imported item classification -------------------------------------------


@dataclass(frozen=True)
class Classification:
    """Category + priority suggestion for an imported issue or pull request."""

    category: str
    category_confidence: str              # low | medium | high
    priority: str
    owner_hint: str | None
    category_reasons: list[str] = field(default_factory=list)
    priority_reasons: list[str] = field(default_factory=list)
    category_source: str = "rules"        # rules | ml | github_label
    category_score: float | None = None   # ML probability, when the ML model decided


def clean_body(body: str | None, limit: int = 8000) -> str:
    """Drop HTML comments (issue-template instructions) and cap the length."""
    return _HTML_COMMENT.sub(" ", body or "")[:limit]


def _label_matches(labels: list[str], candidates: tuple[str, ...]) -> str | None:
    for label in labels:
        if _normalize(label).strip() in candidates:
            return label
    return None


def _classify_category(
    title: str, body: str, kind: str, labels: list[str]
) -> tuple[str, str, list[str]]:
    scores = dict.fromkeys(CATEGORIES, 0)
    title_scores = dict.fromkeys(CATEGORIES, 0)
    reasons: list[str] = []
    negated: list[str] = []

    for category, keywords in CATEGORY_KEYWORDS.items():
        body_points = 0
        for keyword in keywords:
            hit = find_keyword(title, (keyword,))
            if hit.keyword:
                scores[category] += TITLE_WEIGHT
                title_scores[category] += TITLE_WEIGHT
                reasons.append(f"title mentions '{keyword}' (+{TITLE_WEIGHT} {category})")
                continue
            negated.extend(kw for kw in hit.negated if kw not in negated)
            hit = find_keyword(body, (keyword,))
            negated.extend(kw for kw in hit.negated if kw not in negated)
            if hit.keyword and body_points < BODY_CAP:
                body_points += BODY_WEIGHT
                scores[category] += BODY_WEIGHT
                reasons.append(f"body mentions '{keyword}' (+{BODY_WEIGHT} {category})")

        label = _label_matches(labels, CATEGORY_LABELS[category])
        if label:
            scores[category] += LABEL_WEIGHT
            reasons.append(f"GitHub label '{label}' (+{LABEL_WEIGHT} {category})")

    if kind == "pr":
        prefix = CONVENTIONAL_PREFIX.match(title)
        if prefix:
            category = CONVENTIONAL_CATEGORY[prefix.group(1).lower()]
            scores[category] += LABEL_WEIGHT
            reasons.append(f"PR title prefix '{prefix.group(1)}:' (+{LABEL_WEIGHT} {category})")

    if title.rstrip().endswith("?"):
        scores["question"] += 1
        reasons.append("title ends with '?' (+1 question)")

    if negated:
        reasons.append(f"ignored negated {_negation_note(negated)}")

    # Ties go to the category with more title evidence, then to table order (stable sort).
    ranked = sorted(CATEGORIES, key=lambda c: (-scores[c], -title_scores[c]))
    best, runner_up = ranked[0], ranked[1]
    summary = ", ".join(f"{c}={scores[c]}" for c in CATEGORIES)

    if scores[best] == 0:
        best = "feature" if kind == "pr" else "question"
        reasons.append(
            f"no category signals found; defaulting to '{best}' "
            + ("(most PRs add or change behaviour)" if kind == "pr" else "(needs clarification)")
        )
        return best, "low", reasons

    margin = scores[best] - scores[runner_up]
    if margin == 0:
        confidence = "low"
        reasons.append(f"scores: {summary} — tie, picked '{best}' (more title evidence / rule order)")
    else:
        confidence = "high" if scores[best] >= 3 and margin >= 2 else "medium"
        reasons.append(f"scores: {summary}")
    return best, confidence, reasons


def _classify_priority(
    title: str,
    body: str,
    category: str,
    labels: list[str],
    reactions: int,
    comments: int,
) -> tuple[str, list[str]]:
    score = 0
    reasons: list[str] = []
    negated: list[str] = []

    for name, points, keywords in PRIORITY_GROUPS:
        hit = find_keyword(title, keywords)
        negated.extend(kw for kw in hit.negated if kw not in negated)
        where = "title"
        if not hit.keyword:
            body_hit = find_keyword(body, keywords)
            negated.extend(kw for kw in body_hit.negated if kw not in negated)
            if body_hit.keyword:
                hit, where = body_hit, "body"
                points = points - 1 if points > 0 else points + 1
        if hit.keyword:
            score += points
            reasons.append(f"{where} mentions '{hit.keyword}' ({name}, {points:+d})")

    if negated:
        reasons.append(f"ignored negated {_negation_note(negated)}")

    high_label = _label_matches(labels, PRIORITY_HIGH_LABELS)
    if high_label:
        score += 3
        reasons.append(f"GitHub label '{high_label}' (+3)")
    low_label = _label_matches(labels, PRIORITY_LOW_LABELS)
    if low_label:
        score -= 2
        reasons.append(f"GitHub label '{low_label}' (-2)")

    adjust = CATEGORY_PRIORITY_ADJUST.get(category, 0)
    if adjust:
        score += adjust
        reasons.append(f"category '{category}' ({adjust:+d})")

    if reactions >= 10:
        score += 2
        reasons.append(f"{reactions} reactions (+2)")
    elif reactions >= 3:
        score += 1
        reasons.append(f"{reactions} reactions (+1)")
    if comments >= 10:
        score += 1
        reasons.append(f"{comments} comments (+1)")

    if score >= HIGH_THRESHOLD:
        priority = "high"
        rule = f">= {HIGH_THRESHOLD}"
    elif score <= LOW_THRESHOLD:
        priority = "low"
        rule = f"<= {LOW_THRESHOLD}"
    else:
        priority = "medium"
        rule = f"between {LOW_THRESHOLD + 1} and {HIGH_THRESHOLD - 1}"
    reasons.append(f"total score {score:+d} {rule} -> {priority}")
    return priority, reasons


def classify_item(
    title: str,
    body: str | None = None,
    *,
    kind: str = "issue",
    labels: list[str] | None = None,
    reactions: int = 0,
    comments: int = 0,
    category_override: str | None = None,
) -> Classification:
    """Suggest category, priority and an owner hint for an issue or PR.

    Args:
        title: Item title.
        body: Item body (Markdown). HTML comments are ignored.
        kind: ``"issue"`` or ``"pr"``.
        labels: Existing GitHub label names on the item.
        reactions: Total reaction count (a rough signal of how many people care).
        comments: Comment count.
        category_override: Use this category (decided elsewhere, e.g. by the
            ML model) instead of the keyword rules; priority is then computed
            for it. The caller supplies the category explanation.
    """
    labels = labels or []
    title_text = _normalize(title)
    body_text = _normalize(clean_body(body))
    bump_note = None
    if kind == "pr" and DEPENDENCY_BUMP.match(title_text):
        body_text = ""
        bump_note = "dependency-bump PR: body (upstream release notes) ignored"

    if category_override is not None:
        if category_override not in CATEGORIES:
            raise ValueError(f"unknown category: {category_override}")
        category, confidence, category_reasons = category_override, "high", []
    else:
        category, confidence, category_reasons = _classify_category(
            title_text, body_text, kind, labels
        )
    priority, priority_reasons = _classify_priority(
        title_text, body_text, category, labels, reactions, comments
    )

    if bump_note:
        category_reasons.insert(0, bump_note)
        priority_reasons.insert(0, bump_note)

    owner_match, _ = _first_match(f"{title_text} {body_text}", OWNER_KEYWORDS)
    owner_hint = owner_match[0] if owner_match else None

    return Classification(
        category=category,
        category_confidence=confidence,
        priority=priority,
        owner_hint=owner_hint,
        category_reasons=category_reasons,
        priority_reasons=priority_reasons,
    )
