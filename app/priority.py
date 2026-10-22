"""Explainable, deterministic priority scoring for imported issues and PRs.

The result is a **triage suggestion for human review**, not a measurement of
true urgency or business impact. Every factor that influenced it is listed.

How a priority is decided
-------------------------
1. **Maintainer priority label** (e.g. ``P1``, ``priority: high``,
   ``Priority-3``) — a human decision. It is used as-is
   (``source = "maintainer_label"``) and never overridden; the rule-based
   score is still computed and shown "for reference". Conflicting priority
   labels (e.g. ``P1`` and ``P3``) are ignored and the rules decide.
2. **Rule-based score** otherwise (``source = "rules"``): points from the
   factor tables below. Thresholds: score >= 3 -> high, <= -1 -> low, else
   medium.
3. **High needs corroboration.** A high suggestion also requires evidence of
   at least two *different kinds*, at least one of which is severity evidence
   (severe or impact wording, or a severity label). A single keyword — or
   popularity alone — is never enough; such items are capped at medium and the
   explanation says so. Evidence comes from the **title**, labels and
   reactions, not from the body, because bodies often quote templates, logs and
   other discussions: body-only *severe* words add points (one less than in the
   title) but are not evidence, and body-only *impact*/*scope* words are listed
   but not scored.

Every table is defined at the top of this module, so adding a signal is a
one-line change plus a test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.matching import find_all_keywords, negation_note

PRIORITIES = ("low", "medium", "high")
HIGH_THRESHOLD = 3  # score >= 3  -> high (if corroborated)
LOW_THRESHOLD = -1  # score <= -1 -> low

# --- Text signals --------------------------------------------------------------
# (kind, points in title, keywords). Each kind scores at most once. A match found
# only in the body counts one point less (towards zero) — and body-only *impact*
# and *scope* words are not scored at all: in the Phase 3.1 development data
# (docs/PRIORITY_EVALUATION.md) they did not change the maintainer-priority mix
# (25% high vs 32% overall), because bodies quote logs, templates and discussions.
TEXT_SIGNALS: list[tuple[str, int, tuple[str, ...]]] = [
    (
        "severe",
        3,
        (
            "urgent",
            "critical",
            "outage",
            "data loss",
            "data corruption",
            "security",
            "vulnerability",
            "cve",
            "blocker",
            "production down",
            "asap",
        ),
    ),
    (
        "impact",
        2,
        (
            "crash",
            "regression",
            "broken",
            "fail",
            "failure",
            "error",
            "exception",
            "cannot",
            "can't",
            "unable to",
            "500",
            "segfault",
            "panic",
            "hang",
            "freeze",
        ),
    ),
    (
        "scope",
        1,
        (
            "all users",
            "every user",
            "everyone",
            "many users",
            "users can't",
            "users cannot",
            "users are unable",
            "in production",
            "unusable",
        ),
    ),
    (
        "minor",
        -2,
        (
            "typo",
            "nit",
            "minor",
            "cosmetic",
            "someday",
            "nice to have",
            "cleanup",
            "trivial",
            "spelling",
            "wording",
        ),
    ),
]
BODY_UNSCORED = {"impact", "scope"}  # body-only matches: reported, 0 pts
SEVERITY_KINDS = {"severe", "impact", "severity label"}  # can anchor a "high"
EVIDENCE_KINDS = {"severe", "impact", "scope"}  # text kinds counted as evidence

# --- Label signals ---------------------------------------------------------------
# Labels are compared after normalisation: lower-case, runs of space/":"/"/"/"_"/"-"
# collapsed to one space ("Priority: 1 High" -> "priority 1 high").
_PREFIX = re.compile(r"^(?:priority|prio|pri|severity|sev)\s*")

# Words that set a level when the label is prefixed ("priority: high", "sev 1").
PREFIXED_LEVELS = {
    "high": {"0", "1", "p0", "p1", "critical", "urgent", "blocker", "highest", "high"},
    "medium": {"2", "p2", "medium", "normal", "moderate"},
    "low": {"3", "4", "p3", "p4", "low", "lowest", "minor", "trivial"},
}
# Whole labels that set a level without a prefix.
BARE_LEVELS = {
    "high": {"p0", "p1", "critical", "urgent", "blocker", "high priority", "critical priority"},
    "medium": {"p2", "medium priority"},
    "low": {"p3", "p4", "low priority"},
}
# Non-authoritative labels: evidence or adjustments, not decisions.
# A label is a severity topic if it is (or starts with) one of these words, or
# if it is a *type* label containing one — e.g. "regression", "Regression from
# 1.2", "kind/regression", "Severity-Crash". Team/area labels such as
# "sig/security" or "area/crash-reporting" do not count.
SEVERITY_TOPIC_LABELS = {
    "security",
    "vulnerability",
    "regression",
    "crash",
    "data loss",
    "data corruption",
}  # +2, severity evidence
_TYPE_PREFIX = re.compile(r"^(?:kind|type|issue|severity|sev)\s+")
# Note: a "bug" label says what *kind* of item this is, not how severe it is, so it
# is deliberately not used as priority evidence (category and priority stay separate).
LOW_SIGNAL_LABELS = {
    "good first issue": -1,
    "wontfix": -2,
    "won't fix": -2,
    "invalid": -2,
    "duplicate": -2,
}

# --- Other signals -------------------------------------------------------------
# Feature requests are rarely urgent: in the Phase 3.1 development data 57% of
# feature-category issues were maintainer-low and 13% high (docs/PRIORITY_EVALUATION.md).
CATEGORY_ADJUST = {"bug": 1, "feature": -1, "documentation": -1, "question": -1}
REACTIONS_THRESHOLD = 10  # >= 10 reactions: +1 ("many people affected"); never alone enough
DRAFT_PENALTY = -1  # draft PRs are not ready for review yet


@dataclass(frozen=True)
class PriorityResult:
    priority: str  # low | medium | high
    source: str  # rules | maintainer_label
    score: int  # rule-based score (also computed for label decisions)
    reasons: list[str] = field(default_factory=list)
    evidence: tuple[str, ...] = ()  # distinct evidence kinds found


def is_severity_topic_label(label: str) -> bool:
    name = normalize_label(label)
    rest = _TYPE_PREFIX.sub("", name, count=1)
    if rest != name:  # a type label: the topic word may appear anywhere after the prefix
        return any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", rest) for t in SEVERITY_TOPIC_LABELS)
    return any(name == t or name.startswith(t + " ") for t in SEVERITY_TOPIC_LABELS)


def normalize_label(label: str) -> str:
    return re.sub(r"[\s:/_\-]+", " ", str(label).lower().replace("\u2019", "'")).strip()


def label_priority(label: str) -> str | None:
    """The priority a maintainer label explicitly sets, or ``None``."""
    name = normalize_label(label)
    for level, names in BARE_LEVELS.items():
        if name in names:
            return level
    stripped = _PREFIX.sub("", name, count=1)
    if stripped == name or not stripped:
        return None  # no priority/severity prefix
    first = stripped.split()[0]  # "1 high" -> "1"
    for level, words in PREFIXED_LEVELS.items():
        if stripped in words or first in words:
            return level
    return None


def maintainer_priority(labels: list[str]) -> tuple[str | None, list[str], str | None]:
    """Return ``(level, matching_labels, conflict_note)`` from explicit priority labels."""
    found: dict[str, list[str]] = {}
    for label in labels:
        level = label_priority(label)
        if level:
            found.setdefault(level, []).append(label)
    if not found:
        return None, [], None
    if len(found) > 1:
        names = ", ".join(f"'{lbl}'" for lbls in found.values() for lbl in lbls)
        return None, [], f"conflicting maintainer priority labels {names} ignored"
    level, lbls = next(iter(found.items()))
    return level, lbls, None


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def score_priority(
    title: str,
    body: str = "",
    *,
    category: str | None = None,
    category_confident: bool = True,
    kind: str = "issue",
    labels: list[str] | None = None,
    reactions: object = 0,
    is_draft: bool = False,
) -> PriorityResult:
    """Score one item. ``title``/``body`` must already be normalised (lower-case).

    Missing metadata is treated as "no signal": ``None`` labels, reactions or
    category simply contribute nothing. ``category_confident=False`` (a
    low-confidence category guess) skips the category adjustment, so an item
    we know little about stays neutral (medium) rather than drifting to low.
    """
    labels = [lbl for lbl in (labels or []) if lbl]
    score = 0
    reasons: list[str] = []
    evidence: list[str] = []
    negated: list[str] = []
    positive_text, minor_text, body_only = False, False, False

    # 1. Text signals (title beats body; each kind scores once).
    for signal, points, keywords in TEXT_SIGNALS:
        in_title, neg_t = find_all_keywords(title, keywords)
        in_body, neg_b = find_all_keywords(body, keywords) if not in_title else ([], [])
        negated += [kw for kw in neg_t + neg_b if kw not in negated]
        found, where = (in_title, "title") if in_title else (in_body, "body")
        if not found:
            continue
        shown = ", ".join(f"'{kw}'" for kw in found[:3])
        if where == "body" and signal in BODY_UNSCORED:
            reasons.append(f"body mentions {shown} ({signal}; body-only, not scored)")
            continue
        if where == "body" and points != 0:
            points = points - 1 if points > 0 else points + 1
        score += points
        reasons.append(f"{where} mentions {shown} ({signal}, {points:+d})")
        if signal in EVIDENCE_KINDS:
            positive_text = True
            if where == "title":
                evidence.append(signal)
            else:
                body_only = True
        if signal == "minor":
            minor_text = True
    if negated:
        reasons.append(f"ignored negated {negation_note(negated)}")
    if positive_text and minor_text:
        reasons.append("note: both severity and minor wording present (conflicting signals)")
    if body_only:
        reasons.append(
            "note: words found only in the body add points but are not " "counted as evidence"
        )

    # 2. Non-authoritative labels.
    normalized = {normalize_label(lbl): lbl for lbl in labels}
    topic = next((orig for orig in normalized.values() if is_severity_topic_label(orig)), None)
    if topic:
        score += 2
        evidence.append("severity label")
        reasons.append(f"GitHub label '{topic}' (severity topic, +2)")
    for norm, orig in normalized.items():
        if norm in LOW_SIGNAL_LABELS:
            score += LOW_SIGNAL_LABELS[norm]
            reasons.append(f"GitHub label '{orig}' ({LOW_SIGNAL_LABELS[norm]:+d})")

    # 3. Category (a small modifier, not independent evidence: it is usually
    #    derived from the same words already scored above).
    adjust = CATEGORY_ADJUST.get(category or "", 0)
    if adjust and not category_confident:
        reasons.append(f"category '{category}' is a low-confidence guess: no adjustment")
    elif adjust:
        score += adjust
        reasons.append(f"category '{category}' ({adjust:+d})")

    # 4. Engagement: reactions only, capped at +1. Comments are not scored —
    #    long discussions are not evidence of impact.
    n_reactions = _safe_int(reactions)
    if n_reactions >= REACTIONS_THRESHOLD:
        score += 1
        evidence.append("engagement")
        reasons.append(f"{n_reactions} reactions (+1; supports but never sets high on its own)")

    # 5. Pull-request state.
    if kind == "pr" and is_draft:
        score += DRAFT_PENALTY
        reasons.append(f"draft PR, not ready for review ({DRAFT_PENALTY:+d})")

    kinds = tuple(dict.fromkeys(evidence))  # distinct, in order found
    if score >= HIGH_THRESHOLD:
        corroborated = len(kinds) >= 2 and any(k in SEVERITY_KINDS for k in kinds)
        if corroborated:
            priority = "high"
            reasons.append(
                f"total score {score:+d} >= {HIGH_THRESHOLD} with "
                f"{len(kinds)} kinds of evidence ({', '.join(kinds)}) -> high"
            )
        else:
            priority = "medium"
            found = ", ".join(kinds) if kinds else "none"
            reasons.append(
                f"total score {score:+d} >= {HIGH_THRESHOLD}, but high needs two "
                f"kinds of evidence incl. severity (found: {found}) "
                "-> medium (capped)"
            )
    elif score <= LOW_THRESHOLD:
        priority = "low"
        reasons.append(f"total score {score:+d} <= {LOW_THRESHOLD} -> low")
    else:
        priority = "medium"
        reasons.append(
            f"total score {score:+d} between {LOW_THRESHOLD + 1} and "
            f"{HIGH_THRESHOLD - 1} -> medium"
        )
    rules = PriorityResult(priority, "rules", score, reasons, kinds)

    # 6. A maintainer's explicit priority label wins; the rules are shown for reference.
    level, matched, conflict = maintainer_priority(labels)
    if conflict:
        return PriorityResult(priority, "rules", score, [conflict, *reasons], kinds)
    if level:
        names = ", ".join(f"'{lbl}'" for lbl in matched)
        return PriorityResult(
            level,
            "maintainer_label",
            score,
            [
                f"maintainer priority label {names} -> {level} (human decision, kept as-is)",
                f"for reference, the rules alone would suggest {priority} (score {score:+d})",
            ],
            kinds,
        )
    return rules
