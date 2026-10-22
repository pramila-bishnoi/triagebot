"""Tests for the priority evaluation tooling (no network, synthetic data only)."""

from __future__ import annotations

import json

import pytest

from scripts import evaluate_priority as ev
from scripts import fetch_priority_dataset as fpd


def test_metrics_hand_computed():
    gold = ["high", "high", "medium", "low", "low"]
    pred = ["high", "medium", "medium", "high", "low"]
    m = ev.metrics(gold, pred)
    assert m["accuracy"] == pytest.approx(3 / 5)
    assert m["confusion_matrix"] == [[1, 1, 0], [0, 1, 0], [1, 0, 1]]
    assert m["two_level_errors"] == 1  # one low predicted as high
    assert m["mean_abs_level_error"] == pytest.approx((0 + 1 + 0 + 2 + 0) / 5)
    assert m["per_level"]["high"] == pytest.approx(
        {"precision": 0.5, "recall": 0.5, "f1": 0.5, "support": 2, "predicted": 2}
    )
    assert m["predicted_distribution"] == {"high": 2, "medium": 2, "low": 1}


def test_metrics_empty():
    assert ev.metrics([], [])["accuracy"] == 0.0


@pytest.mark.parametrize(
    ("label", "level"),
    [
        ("Priority-1", "high"),
        ("priority: 0 crash", "high"),
        ("priority: 2 medium", "medium"),
        ("Priority-3", "low"),
        ("high priority", "high"),
        ("A11ySev2", None),
        ("Severity-Crash", None),
        ("status: critical", None),
    ],
)
def test_legacy_gold_mapping(label, level):
    assert ev.legacy_gold_level(label) == level


def _examples():
    rows = [
        {"repo": "a/dev", "split": "dev", "number": 1, "gold": "high"},
        {"repo": "a/dev", "split": "dev", "number": 2, "gold": "low"},
        {"repo": "b/test", "split": "test", "number": 1, "gold": "low"},
    ]
    return rows


def test_evaluate_groups_by_split_and_repo():
    examples = _examples()
    preds = {"a/dev#1": "high", "a/dev#2": "high", "b/test#1": "low"}
    result = ev.evaluate(examples, preds)
    assert result["dev"]["accuracy"] == pytest.approx(0.5)
    assert result["test"]["accuracy"] == 1.0
    assert result["repo:a/dev"]["n"] == 2
    assert result["all"]["n"] == 3


def test_baseline_majority_uses_development_data_only():
    runs = ev.baselines(_examples())
    # dev has one high and one low: a tie resolved by first occurrence ('high');
    # the test split's label must not influence the choice.
    assert "development majority ('high')" in runs
    assert set(runs["always medium"].values()) == {"medium"}


def test_rules_pipeline_is_deterministic():
    examples = [
        {
            "repo": "x/y",
            "number": 1,
            "title": "App crashes for all users",
            "body": "",
            "labels": [],
            "reactions": 0,
            "comments": 0,
        }
    ]
    first = ev.predict(examples, "rules")
    assert first == ev.predict(examples, "rules") == {"x/y#1": "high"}


def test_build_hides_reference_label_and_drops_conflicts(tmp_path, monkeypatch):
    monkeypatch.setattr(fpd, "RAW_DIR", tmp_path)
    monkeypatch.setattr(fpd, "REPOS", {"o/r": ("dev", {"P1": "high", "P3": "low"})})

    def issue(number, labels):
        return {
            "number": number,
            "title": f"t{number}",
            "body": "",
            "state": "open",
            "labels": [{"name": n} for n in labels],
            "comments": 0,
            "reactions": {"total_count": 0},
            "created_at": "2026-01-01T00:00:00Z",
            "html_url": f"https://github.com/o/r/issues/{number}",
        }

    (tmp_path / "o__r__P1.json").write_text(
        json.dumps(
            [
                issue(1, ["P1", "bug"]),
                issue(2, ["P1", "P3"]),
                {**issue(3, ["P1"]), "pull_request": {}},
            ]
        )
    )
    (tmp_path / "o__r__P3.json").write_text(json.dumps([issue(2, ["P1", "P3"]), issue(4, ["p3"])]))
    rows = fpd.build()
    assert [(r["number"], r["level"]) for r in rows] == [(1, "high"), (4, "low")]
    assert rows[0]["labels"] == ["bug"]  # reference label removed
    assert rows[1]["labels"] == []
    assert all(r["split"] == "dev" for r in rows)


def test_per_label_budget_splits_levels_evenly():
    counts = fpd._per_label({"P0": "high", "P1": "high", "P2": "medium"})
    assert counts == {"P0": fpd.PER_LEVEL // 2, "P1": fpd.PER_LEVEL // 2, "P2": fpd.PER_LEVEL}


def test_split_is_by_repository():
    splits = {split for split, _ in fpd.REPOS.values()}
    assert splits == {"dev", "test", "dev2", "test2"}
    # Phase 6 rule: the expansion repositories alternate dev2 / test2 by name.
    expansion = sorted((r for r, (s, _) in fpd.REPOS.items() if s.endswith("2")), key=str.lower)
    assert [fpd.REPOS[r][0] for r in expansion] == [
        ("dev2", "test2")[i % 2] for i in range(len(expansion))
    ]
    # No repository is in two splits, and no level mapping is empty.
    for _split, labels in fpd.REPOS.values():
        assert set(labels.values()) <= {"high", "medium", "low"}


def test_combined_groups_only_with_the_expansion():
    def ex(repo, split, gold):
        return {"repo": repo, "number": 1, "split": split, "gold": gold}

    def run(examples):
        return ev.evaluate(examples, {ev.key(e): "low" for e in examples})

    v1 = [ex("a/a", "dev", "high"), ex("b/b", "test", "low")]
    assert "all held-out" not in run(v1)  # the v1 report keeps its old groups
    expanded = [*v1, ex("c/c", "dev2", "medium"), ex("d/d", "test2", "low")]
    res = run(expanded)
    assert res["all held-out"]["n"] == 2 and res["all development"]["n"] == 2


def test_split_error_columns_are_directional():
    m = ev.metrics(["high", "low", "low"], ["low", "high", "high"])
    runs = [("x", {"g": m})]
    table = ev._summary(runs, "g", split_errors=True)
    assert "high→low / low→high" in table and "| 1 / 2 |" in table
