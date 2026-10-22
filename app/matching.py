"""Text normalisation and keyword matching shared by the rule engines.

Used by :mod:`app.triage` (category rules) and :mod:`app.priority` (priority
scoring). Keywords match whole words only (``auth`` does not match "author",
``500`` does not match "1500ms"), allowing simple inflections (``crash``
matches "crashes", "crashed", "crashing"). A keyword directly preceded by a
negation such as "not", "no", "never" or "non-" (optionally with a filler
word, as in "not a bug" or "not very urgent") is ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

_NEGATION = re.compile(
    r"(?:^|[^\w'])(?:not|no|never|without|non|isn't|aren't|wasn't|isnt)[\s-]+"
    r"(?:(?:a|an|the|really|very|so|too|that|yet|actually)\s+)?$"
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@lru_cache(maxsize=1024)
def _pattern(keyword: str) -> re.Pattern[str]:
    """Whole-word pattern that tolerates simple English inflections."""
    return re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?:s|es|ed|d|ing)?(?!\w)")


def normalize(text: str) -> str:
    return text.lower().replace("’", "'")


def clean_body(body: str | None, limit: int = 8000) -> str:
    """Drop HTML comments (issue-template instructions) and cap the length."""
    return _HTML_COMMENT.sub(" ", body or "")[:limit]


@dataclass(frozen=True)
class Hit:
    """Result of searching text for a list of keywords."""

    keyword: str | None = None  # first non-negated keyword found
    negated: tuple[str, ...] = ()  # keywords seen but cancelled by a negation


def find_keyword(text: str, keywords: tuple[str, ...]) -> Hit:
    """Return the first keyword (in table order) that occurs un-negated in ``text``.

    ``text`` must already be normalised (lower-case).
    """
    negated: list[str] = []
    for keyword in keywords:
        for match in _pattern(keyword).finditer(text):
            preceding = text[max(0, match.start() - 40) : match.start()]
            if _NEGATION.search(preceding):
                if keyword not in negated:
                    negated.append(keyword)
                continue
            return Hit(keyword, tuple(negated))
    return Hit(None, tuple(negated))


def find_all_keywords(text: str, keywords: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Return ``(found, negated)``: every keyword occurring un-negated, in table order."""
    found: list[str] = []
    negated: list[str] = []
    for keyword in keywords:
        hit = find_keyword(text, (keyword,))
        if hit.keyword:
            found.append(keyword)
        elif hit.negated:
            negated.append(keyword)
    return found, negated


def negation_note(negated: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f"'{kw}'" for kw in negated)
