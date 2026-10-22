"""Explainable duplicate-issue detection (TF-IDF + cosine similarity).

How it works
------------
1. **Normalise** each issue's title and description: drop HTML comments,
   template headings and checklists, URLs, Markdown/HTML markup and code-fence
   markers (code *content* is kept — identical error messages are strong
   evidence), lower-case, collapse whitespace. Technical tokens such as
   ``read_csv``, ``tsconfig.json`` or ``0xc000041d`` are kept whole.
2. **Vectorise** titles and descriptions separately with TF-IDF fitted on the
   issues of *one repository* (so words common in that repository, such as its
   own name, count for little).
3. **Score** every pair with cosine similarity:
   ``score = TITLE_WEIGHT · title_similarity + (1 − TITLE_WEIGHT) · description_similarity``.
   If either description is (nearly) empty, the title similarity is used alone
   and the explanation says so.
4. **Suggest** at most ``MAX_SUGGESTIONS`` other issues per issue whose score is
   at least the threshold. Issues are never compared with themselves, pull
   requests are not compared, and very short texts are skipped.

The score is a **similarity**, not the probability that two issues are
duplicates. Thresholds and the title weight were chosen on development
repositories (``docs/DUPLICATE_EVALUATION.md``). Suggestions are only ever
shown to a maintainer for review; nothing is changed on GitHub.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

ALGORITHM_VERSION = 1
TITLE_WEIGHT = 0.6  # chosen on development data (see docs/DUPLICATE_EVALUATION.md)
DEFAULT_THRESHOLD = 0.2  # chosen on development data (see docs/DUPLICATE_EVALUATION.md)
MAX_SUGGESTIONS = 5
MIN_TITLE_TOKENS = 2  # titles shorter than this (after cleaning) are not compared
MIN_BODY_TOKENS = 5  # descriptions shorter than this are treated as empty
MAX_CHARS = 5000
BLOCK_ROWS = 500  # rows per block when computing pairwise similarities

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s.*$", re.MULTILINE)
_CHECKBOX = re.compile(r"^\s*[-*+]\s*\[[ xX]\].*$", re.MULTILINE)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_FENCE = re.compile(r"```[\w+-]*|~~~")
_QUOTE = re.compile(r"^\s*>+", re.MULTILINE)
# Emphasis markers only at word boundaries, so "read_csv" keeps its underscore.
_EMPHASIS = re.compile(r"(?<!\w)[*_]{1,3}(?=\w)|(?<=\w)[*_]{1,3}(?!\w)")
_MARKUP_CHARS = re.compile(r"[`|]")
_WHITESPACE = re.compile(r"\s+")
# Words of 2+ characters; dots, hyphens and underscores inside a token are kept
# ("tsconfig.json", "read_csv", "utf-8").
TOKEN_PATTERN = r"(?u)\b\w[\w.\-]*\w\b"
_TOKEN = re.compile(TOKEN_PATTERN)


def normalize_text(text: str | None) -> str:
    """Clean one title or description for similarity comparison."""
    text = text or ""
    text = _HTML_COMMENT.sub(" ", text)
    text = _HEADING.sub(" ", text)
    text = _CHECKBOX.sub(" ", text)
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    text = _FENCE.sub(" ", text)
    text = _QUOTE.sub(" ", text)
    text = _EMPHASIS.sub(" ", text)
    text = _MARKUP_CHARS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip().lower()[:MAX_CHARS]


def token_count(text: str) -> int:
    return len(_TOKEN.findall(text))


@dataclass(frozen=True)
class Match:
    """One suggested pair, with everything needed to explain it."""

    a: int  # index into the issue list (a < b)
    b: int
    score: float
    title_similarity: float
    body_similarity: float | None  # None when a description was missing or too short
    shared_terms: list[str] = field(default_factory=list)

    def explanation(self) -> list[str]:
        lines = [f"title similarity {self.title_similarity:.2f}"]
        if self.body_similarity is None:
            lines.append("description missing or too short on one side: title only")
        else:
            lines.append(f"description similarity {self.body_similarity:.2f}")
        if self.shared_terms:
            lines.append("shared terms: " + ", ".join(f"'{t}'" for t in self.shared_terms))
        lines.append(
            f"combined similarity {self.score:.2f} (a similarity score, " "not a probability)"
        )
        return lines


def _vectorizer() -> TfidfVectorizer:
    return TfidfVectorizer(
        token_pattern=TOKEN_PATTERN,
        ngram_range=(1, 2),
        sublinear_tf=True,
        stop_words="english",
        min_df=1,
        dtype=np.float32,
    )


class DuplicateIndex:
    """TF-IDF index over the issues of one repository."""

    def __init__(self, issues: list[dict], *, title_weight: float = TITLE_WEIGHT) -> None:
        """``issues``: dicts with ``title`` and ``body`` (other keys are ignored)."""
        self.issues = issues
        self.title_weight = title_weight
        self.titles = [normalize_text(i.get("title")) for i in issues]
        self.bodies = [normalize_text(i.get("body")) for i in issues]
        self.usable = np.array([token_count(t) >= MIN_TITLE_TOKENS for t in self.titles])
        self.has_body = np.array([token_count(b) >= MIN_BODY_TOKENS for b in self.bodies])
        self._title_vec = _vectorizer()
        self._body_vec = _vectorizer()
        self.T = self._fit(self._title_vec, self.titles)
        self.B = self._fit(self._body_vec, self.bodies)

    @staticmethod
    def _fit(vectorizer: TfidfVectorizer, texts: list[str]):
        try:
            return vectorizer.fit_transform(texts).tocsr()
        except ValueError:  # empty vocabulary (e.g. all texts empty or stop words)
            return None

    def _similarities(self, rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        n = len(self.issues)
        title = (
            np.asarray((self.T[rows] @ self.T.T).todense())
            if self.T is not None
            else np.zeros((len(rows), n), dtype=np.float32)
        )
        body = (
            np.asarray((self.B[rows] @ self.B.T).todense())
            if self.B is not None
            else np.zeros((len(rows), n), dtype=np.float32)
        )
        return title, body

    def _combine(self, rows: np.ndarray, title: np.ndarray, body: np.ndarray) -> np.ndarray:
        both = self.has_body[rows][:, None] & self.has_body[None, :]
        combined = np.where(both, self.title_weight * title + (1 - self.title_weight) * body, title)
        combined[:, ~self.usable] = -1.0
        combined[~self.usable[rows], :] = -1.0
        combined[np.arange(len(rows)), rows] = -1.0  # never match an issue with itself
        return combined

    def _shared_terms(self, i: int, j: int, limit: int = 5) -> list[str]:
        terms: dict[str, float] = {}
        for matrix, vec in ((self.T, self._title_vec), (self.B, self._body_vec)):
            if matrix is None:
                continue
            product = matrix[i].multiply(matrix[j]).tocoo()
            names = vec.get_feature_names_out()
            for col, value in zip(product.col, product.data, strict=True):
                terms[names[col]] = terms.get(names[col], 0.0) + float(value)
        ranked = [t for t, _ in sorted(terms.items(), key=lambda kv: (-kv[1], kv[0]))]
        # Prefer phrases: drop a single word that is part of a shown two-word phrase.
        top = ranked[: limit * 3]
        in_phrases = {w for t in top if " " in t for w in t.split()}
        return [t for t in top if " " in t or t not in in_phrases][:limit]

    def _match(self, i: int, j: int, score: float, title: float, body: float) -> Match:
        i, j = int(i), int(j)
        a, b = (i, j) if i < j else (j, i)
        both = bool(self.has_body[i] and self.has_body[j])
        return Match(
            a,
            b,
            round(float(score), 4),
            round(float(title), 4),
            round(float(body), 4) if both else None,
            self._shared_terms(a, b),
        )

    def rank(self, i: int, top: int = MAX_SUGGESTIONS) -> list[Match]:
        """The ``top`` most similar issues to issue ``i`` (no threshold), best first."""
        rows = np.array([i])
        title, body = self._similarities(rows)
        combined = self._combine(rows, title, body)[0]
        order = [j for j in np.argsort(-combined, kind="stable")[:top] if combined[j] > 0]
        return [self._match(i, j, combined[j], title[0, j], body[0, j]) for j in order]

    def pairs(
        self, threshold: float = DEFAULT_THRESHOLD, per_issue: int = MAX_SUGGESTIONS
    ) -> list[Match]:
        """All pairs above ``threshold``, keeping each issue's ``per_issue`` best."""
        n = len(self.issues)
        best: dict[tuple[int, int], tuple[float, float, float]] = {}
        for start in range(0, n, BLOCK_ROWS):
            rows = np.arange(start, min(n, start + BLOCK_ROWS))
            title, body = self._similarities(rows)
            combined = self._combine(rows, title, body)
            for r, i in enumerate(rows):
                candidates = np.flatnonzero(combined[r] >= threshold)
                if candidates.size == 0:
                    continue
                top = candidates[np.argsort(-combined[r, candidates], kind="stable")][:per_issue]
                for j in top:
                    key = (int(min(i, j)), int(max(i, j)))
                    best[key] = (combined[r, j], title[r, j], body[r, j])
        return [self._match(a, b, *best[(a, b)]) for a, b in sorted(best)]


# --- Repository analysis (database integration) -------------------------------------


def current_threshold() -> float:
    from app.config import get_settings

    configured = get_settings().duplicate_threshold
    return DEFAULT_THRESHOLD if configured is None else configured


def fingerprint(issues: list[dict], threshold: float, title_weight: float = TITLE_WEIGHT) -> str:
    """Changes whenever an issue is added, removed or edited, or the settings change."""
    digest = hashlib.sha256(f"v{ALGORITHM_VERSION}|{threshold:.4f}|{title_weight:.4f}|".encode())
    for issue in sorted(issues, key=lambda i: i["id"]):
        digest.update(
            f"{issue['id']}\x1f{issue.get('title') or ''}\x1f"
            f"{issue.get('body') or ''}\x1e".encode()
        )
    return digest.hexdigest()


def analyze_repo(repo_id: int, *, force: bool = False, threshold: float | None = None) -> dict:
    """Find possible duplicates among a repository's imported issues and store them.

    Skipped (``reused=True``) when nothing changed since the last analysis,
    unless ``force``. Maintainer decisions are always preserved.
    """
    from app import repository

    threshold = current_threshold() if threshold is None else threshold
    issues = repository.issues_for_duplicates(repo_id)
    stamp = fingerprint(issues, threshold)
    summary = {"issues": len(issues), "threshold": threshold, "reused": False}
    if not force and repository.get_duplicates_state(repo_id) == stamp:
        counts = repository.duplicate_counts(repo_id)
        return {**summary, "reused": True, **counts, "compared": None, "skipped_short": None}

    index = DuplicateIndex(issues)
    pairs = [
        {
            "item_a": issues[m.a]["id"],
            "item_b": issues[m.b]["id"],
            "score": m.score,
            "title_similarity": m.title_similarity,
            "body_similarity": m.body_similarity,
            "shared_terms": m.shared_terms,
        }
        for m in index.pairs(threshold)
    ]
    counts = repository.save_duplicate_analysis(repo_id, pairs, stamp)
    usable = int(index.usable.sum()) if issues else 0
    return {**summary, **counts, "compared": usable, "skipped_short": len(issues) - usable}


def explain(suggestion: dict) -> list[str]:
    """Human-readable reasons for a stored suggestion."""
    match = Match(
        0,
        1,
        suggestion["score"],
        suggestion["title_similarity"],
        suggestion["body_similarity"],
        suggestion.get("shared_terms") or [],
    )
    return match.explanation()
