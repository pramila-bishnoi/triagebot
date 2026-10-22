"""Tests for the duplicate evaluation tooling (synthetic data, no network)."""

from __future__ import annotations

import json

import pytest

from scripts import evaluate_duplicates as ev
from scripts import fetch_duplicate_dataset as fdd


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("Duplicate of #48441, your own issue.", 48441),
        ("/dup #123", 123),
        ("dupe of #9", 9),
        ("closing as a duplicate of https://github.com/a/b/issues/77", 77),
        ("duplicate of https://github.com/x/y/issues/5", None),  # another repository
        ("dup #4, sorry: actually duplicate of #8", 8),  # last mention wins
        ("no mention here", None),
    ],
)
def test_canonical_number(text, number):
    assert fdd.canonical_number(text, "a/b") == number


def _query(truth, ranked, split="dev"):
    return {"split": split, "truth": set(truth), "ranked": ranked}


def test_metrics_hand_computed():
    queries = [
        _query({10}, [(10, 0.9), (11, 0.6)]),  # original first, one extra suggestion
        _query({20}, [(21, 0.7), (20, 0.55)]),  # original second
        _query({30}, [(31, 0.4)]),  # original not ranked at all
    ]
    m = ev.metrics(queries, negative_scores=[[0.6], [0.3], []], threshold=0.5)
    assert m["recall_at_1"] == pytest.approx(1 / 3)
    assert m["recall_at_5"] == pytest.approx(2 / 3)
    assert m["mrr"] == pytest.approx((1 + 1 / 2 + 0) / 3)
    assert m["suggestions"] == 4  # 0.9, 0.6, 0.7, 0.55
    assert m["precision"] == pytest.approx(2 / 4)
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["f1"] == pytest.approx(2 * 0.5 * (2 / 3) / (0.5 + 2 / 3))
    assert m["false_alarm_rate"] == pytest.approx(1 / 3)


def test_select_prefers_higher_threshold_on_ties():
    q = [_query({1}, [(1, 0.9)])]
    results = {0.6: (q, {"dev": [], "test": []}), 1.0: (q, {"dev": [], "test": []})}
    weight, threshold, best = ev.select(results)
    assert best["f1"] == 1.0
    assert threshold == max(t for t in ev.THRESHOLDS if t <= 0.9)
    assert weight == 1.0


def test_end_to_end_on_synthetic_pairs(tmp_path, monkeypatch):
    crash = "Settings page crashes when saving profile"
    body = "Clicking save crashes the app with NullReferenceException in ProfileForm.save"
    pairs = [
        {
            "repo": "o/dev",
            "split": "dev",
            "duplicate": {"number": 2, "title": crash, "body": body, "html_url": "u"},
            "canonical": {"number": 1, "title": crash + " again", "body": body, "html_url": "u"},
        }
    ]
    path = tmp_path / "pairs.jsonl"
    path.write_text("\n".join(json.dumps(p) for p in pairs))
    monkeypatch.setattr(ev, "PAIRS_FILE", path)
    monkeypatch.setattr(
        ev,
        "cached_issues",
        lambda: {
            ("o/dev", 3): {"title": "Add dark mode to the docs site", "body": "Dark theme please."},
            ("o/dev", 4): {"title": "Upgrade numpy to 2.0", "body": "Bump the minimum version."},
            ("o/dev", 5): {"title": "Add dark mode to the docs site", "body": "Dark theme please."},
        },
    )
    loaded, pools, negatives = ev.load("dev")
    assert set(pools["o/dev"]) == {1, 2, 3, 4, 5}
    assert negatives["o/dev"] == [3, 4, 5]  # known pair members are never negatives
    queries, negs = ev.rank_queries(loaded, pools, negatives, 0.6)
    assert queries[0]["ranked"][0][0] == 1  # the original ranks first
    assert all(isinstance(s, float) for scores in negs for s in scores)
    m = ev.metrics(queries, negs, 0.5)
    # 3 and 5 are identical but unlabelled: each counts as a false alarm
    assert (m["recall_at_1"], m["recall"]) == (1.0, 1.0)
    assert m["false_alarm_rate"] == pytest.approx(2 / 3)


# --- Phase 6 expansion -------------------------------------------------------------------


def test_cohort_filter_keeps_phase4_reproducible(tmp_path, monkeypatch):
    def pair(n, cohort=None):
        p = {
            "repo": "o/r",
            "split": "dev",
            "duplicate": {"number": n, "title": f"Crash {n} on save", "body": "", "html_url": "u"},
            "canonical": {
                "number": 100 + n,
                "title": f"Crash {n} when saving",
                "body": "",
                "html_url": "u",
            },
        }
        if cohort:
            p["cohort"] = cohort
        return p

    path = tmp_path / "pairs.jsonl"
    path.write_text("\n".join(json.dumps(p) for p in [pair(1), pair(2, "phase6")]))
    monkeypatch.setattr(ev, "PAIRS_FILE", path)
    monkeypatch.setattr(
        ev,
        "cached_issues",
        lambda: {("o/r", 50): {"title": "Unrelated feature request", "body": ""}},
    )
    phase4, pools4, negs4 = ev.load("dev", "phase4")
    assert [p["duplicate"]["number"] for p in phase4] == [1]  # no cohort field = phase4
    assert 2 not in pools4["o/r"]
    everything, pools_all, _ = ev.load("dev", "all")
    assert len(everything) == 2 and {2, 102} <= set(pools_all["o/r"])


@pytest.mark.parametrize(
    ("k", "n", "low", "high"), [(8, 20, 0.219, 0.613), (13, 21, 0.409, 0.792), (0, 10, 0.0, 0.278)]
)
def test_wilson_interval_matches_reference_values(k, n, low, high):
    lo, hi = ev.wilson(k, n)
    assert (round(lo, 3), round(hi, 3)) == (low, high)


def test_frozen_splits_do_not_move_when_pairs_are_added():
    pairs = [{"repo": "pandas-dev/pandas"}, {"repo": "microsoft/terminal"}]
    pairs += [{"repo": "pypa/pip"} for _ in range(40)]  # counts change a lot
    fdd.assign_splits(pairs)
    assert [p["split"] for p in pairs[:3]] == ["test", "dev", "test"]
    with pytest.raises(ValueError):
        fdd.assign_splits([{"repo": "someone/else"}])


def test_expansion_candidates_are_round_robin_and_capped(monkeypatch):
    monkeypatch.setattr(fdd, "EXPANSION_REPOS", {"a/x": None, "b/y": "dup"})
    monkeypatch.setattr(fdd, "EXPANSION_PER_REPO", 2)
    found = {("a/x", n): {} for n in (1, 2, 3)} | {("b/y", n): {} for n in (7, 8)}
    order = fdd.expansion_candidates(found, already={("a/x", 3)})
    assert order == [("a/x", 2), ("b/y", 8), ("a/x", 1), ("b/y", 7)]
