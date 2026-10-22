"""Unit tests for the duplicate-detection engine (app/duplicates.py)."""

from __future__ import annotations

import pytest

from app import duplicates
from app.duplicates import MAX_SUGGESTIONS, DuplicateIndex, fingerprint, normalize_text, token_count

SETTINGS_CRASH = {
    "title": "Settings page crashes when saving profile",
    "body": "Clicking save on the profile settings page crashes the app with "
    "NullReferenceException in ProfileForm.save",
}
PARAPHRASE = {
    "title": "App crashes when saving the profile settings",
    "body": "Saving profile settings throws NullReferenceException in ProfileForm.save "
    "and the app closes",
}
SAME_WORDS_OTHER_PROBLEM = {
    "title": "Settings page loads slowly when saving profile",
    "body": "The profile settings page takes ten seconds to load after saving because of "
    "an N+1 query in the API",
}
DARK_MODE = {
    "title": "Add dark mode to the documentation site",
    "body": "It would be nice to have a dark theme for the docs website.",
}
NUMPY = {
    "title": "Upgrade numpy dependency to 2.0",
    "body": "numpy 2.0 was released; we should bump the minimum version in setup.cfg.",
}


def score(index: DuplicateIndex, i: int, j: int) -> float:
    for match in index.rank(i, top=len(index.issues)):
        if {match.a, match.b} == {i, j}:
            return match.score
    return 0.0


# --- Normalisation ------------------------------------------------------------------


def test_normalize_removes_noise():
    text = normalize_text(
        "### Describe the bug\n<!-- template hint -->\n- [x] I searched existing issues\n"
        "> quoted reply\nSee https://example.com/page and ![img](a.png) [docs](https://x.y)\n"
        "<details>**Expected** | col |</details>"
    )
    for noise in (
        "describe the bug",
        "template hint",
        "searched existing",
        "https",
        "example.com",
        "img",
        "<details>",
        "**",
        "|",
    ):
        assert noise not in text
    assert "quoted reply" in text and "docs" in text and "expected" in text


def test_normalize_keeps_technical_terms_and_code():
    text = normalize_text(
        "Calling `pd.read_csv('a.csv')` fails\n```python\n"
        "TypeError: bad dtype 0xc000041d\n```\ntsconfig.json, utf-8"
    )
    for term in ("read_csv", "typeerror", "0xc000041d", "tsconfig.json", "utf-8"):
        assert term in text
    assert "```" not in text and "`" not in text


@pytest.mark.parametrize("value", [None, "", "   \n\t "])
def test_normalize_empty(value):
    assert normalize_text(value) == ""


def test_normalize_caps_length():
    assert len(normalize_text("word " * 5000)) <= duplicates.MAX_CHARS


def test_token_count():
    assert token_count("read_csv fails on tsconfig.json") == 4
    assert token_count("a") == 0


# --- Similarity ---------------------------------------------------------------------


def test_identical_issues_score_one():
    index = DuplicateIndex([SETTINGS_CRASH, dict(SETTINGS_CRASH), DARK_MODE])
    assert score(index, 0, 1) == pytest.approx(1.0)


def test_ranking_paraphrase_above_same_words_other_problem_above_unrelated():
    index = DuplicateIndex([SETTINGS_CRASH, PARAPHRASE, SAME_WORDS_OTHER_PROBLEM, DARK_MODE, NUMPY])
    paraphrase = score(index, 0, 1)
    other_problem = score(index, 0, 2)
    unrelated = max(score(index, 0, 3), score(index, 0, 4))
    assert paraphrase > other_problem > unrelated
    assert unrelated == 0.0


def test_similar_wording_with_different_meaning_is_a_known_limitation():
    # "crashes when saving" vs "loads slowly when saving": shared wording, different
    # problem. At the default threshold it IS suggested (word overlap cannot see
    # meaning) — but below the true paraphrase, and the explanation shows why, so a
    # maintainer can reject it.
    index = DuplicateIndex([SETTINGS_CRASH, PARAPHRASE, SAME_WORDS_OTHER_PROBLEM, DARK_MODE])
    assert score(index, 0, 1) > score(index, 0, 2) >= duplicates.DEFAULT_THRESHOLD
    match = next(m for m in index.rank(0) if {m.a, m.b} == {0, 2})
    assert any("settings" in term for term in match.shared_terms)
    assert all(3 not in (m.a, m.b) for m in index.pairs())  # unrelated issue never paired


def test_unrelated_issues_produce_no_pairs():
    assert DuplicateIndex([DARK_MODE, NUMPY, SETTINGS_CRASH]).pairs(threshold=0.1) == []


def test_never_matches_an_issue_with_itself():
    index = DuplicateIndex([SETTINGS_CRASH, dict(SETTINGS_CRASH), PARAPHRASE])
    for i in range(3):
        assert all(i != other for m in index.rank(i) for other in ([m.b] if m.a == i else [m.a]))
    assert all(m.a < m.b for m in index.pairs(threshold=0.1))


def test_short_and_empty_texts_are_skipped_gracefully():
    issues = [
        SETTINGS_CRASH,
        {"title": "bug", "body": ""},
        {"title": "", "body": "crash"},
        {"title": None, "body": None},
        dict(SETTINGS_CRASH),
    ]
    index = DuplicateIndex(issues)
    assert index.usable.tolist() == [True, False, False, False, True]
    assert [(m.a, m.b) for m in index.pairs(threshold=0.1)] == [(0, 4)]


@pytest.mark.parametrize(
    "issues", [[], [{"title": "", "body": ""}], [{"title": "the a an", "body": "of the"}]]
)
def test_degenerate_inputs(issues):
    assert DuplicateIndex(issues).pairs(threshold=0.0) == []


def test_missing_description_uses_title_only():
    no_body = {"title": SETTINGS_CRASH["title"], "body": ""}
    index = DuplicateIndex([SETTINGS_CRASH, no_body, DARK_MODE])
    match = index.rank(0)[0]
    assert match.body_similarity is None
    assert match.score == pytest.approx(match.title_similarity)
    assert any("title only" in line for line in match.explanation())


def test_title_weight_is_applied():
    index = DuplicateIndex([SETTINGS_CRASH, PARAPHRASE, DARK_MODE], title_weight=0.6)
    m = index.rank(0)[0]
    assert m.score == pytest.approx(0.6 * m.title_similarity + 0.4 * m.body_similarity, abs=1e-3)


def test_explanation_lists_shared_terms_and_disclaimer():
    m = DuplicateIndex([SETTINGS_CRASH, PARAPHRASE, DARK_MODE]).rank(0)[0]
    lines = m.explanation()
    assert lines[0].startswith("title similarity")
    assert any(line.startswith("shared terms:") for line in lines)
    assert "nullreferenceexception" in " ".join(m.shared_terms)
    assert "not a probability" in lines[-1]


def test_shared_terms_prefer_phrases_without_repeating_words():
    m = DuplicateIndex([SETTINGS_CRASH, dict(SETTINGS_CRASH), DARK_MODE]).rank(0)[0]
    words_in_phrases = {w for t in m.shared_terms if " " in t for w in t.split()}
    assert not any(t in words_in_phrases for t in m.shared_terms if " " not in t)


def test_suggestions_per_issue_are_capped_and_ordered():
    clones = [dict(SETTINGS_CRASH) for _ in range(MAX_SUGGESTIONS + 3)]
    ranked = DuplicateIndex(clones).rank(0)
    assert len(ranked) == MAX_SUGGESTIONS
    assert [m.score for m in ranked] == sorted((m.score for m in ranked), reverse=True)


def test_block_processing_matches_single_block(monkeypatch):
    issues = [
        SETTINGS_CRASH,
        PARAPHRASE,
        SAME_WORDS_OTHER_PROBLEM,
        DARK_MODE,
        NUMPY,
        dict(SETTINGS_CRASH),
        dict(DARK_MODE),
    ]
    full = DuplicateIndex(issues).pairs(threshold=0.2)
    monkeypatch.setattr(duplicates, "BLOCK_ROWS", 2)
    assert DuplicateIndex(issues).pairs(threshold=0.2) == full


def test_deterministic():
    issues = [SETTINGS_CRASH, PARAPHRASE, SAME_WORDS_OTHER_PROBLEM, DARK_MODE]
    assert DuplicateIndex(issues).pairs(threshold=0.2) == DuplicateIndex(issues).pairs(
        threshold=0.2
    )


# --- Fingerprint -------------------------------------------------------------------


def _issues():
    return [{"id": 1, **SETTINGS_CRASH}, {"id": 2, **PARAPHRASE}]


def test_fingerprint_stable_and_order_independent():
    assert fingerprint(_issues(), 0.5) == fingerprint(list(reversed(_issues())), 0.5)


@pytest.mark.parametrize(
    "change",
    [
        lambda xs: xs[0].update(title="Other"),
        lambda xs: xs[1].update(body="changed"),
        lambda xs: xs.pop(),
        lambda xs: xs.append({"id": 3, "title": "new", "body": ""}),
    ],
)
def test_fingerprint_changes_with_issues(change):
    issues = _issues()
    before = fingerprint(issues, 0.5)
    change(issues)
    assert fingerprint(issues, 0.5) != before


def test_fingerprint_changes_with_settings():
    assert fingerprint(_issues(), 0.5) != fingerprint(_issues(), 0.6)
    assert fingerprint(_issues(), 0.5, 0.6) != fingerprint(_issues(), 0.5, 0.7)
