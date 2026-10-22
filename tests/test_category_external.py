"""The external category test set is built with the same rules as the training data."""

from __future__ import annotations

import json

from scripts import evaluate_category_generalization as gen
from scripts import fetch_category_external as fce
from scripts import fetch_dataset


def _raw(number, *labels, pr=False):
    raw = {
        "number": number,
        "title": f"Issue {number}",
        "body": "",
        "html_url": "u",
        "created_at": "2026-08-01T00:00:00Z",
        "labels": [{"name": n} for n in labels],
    }
    if pr:
        raw["pull_request"] = {}
    return raw


def test_build_drops_prs_and_ambiguous_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(fce, "RAW_DIR", tmp_path)
    monkeypatch.setattr(
        fce,
        "REPOS",
        {"o/r": {"bug": "Bug", "feature": "Feature", "documentation": "Docs", "question": "Q"}},
    )
    pages = {
        "bug": [_raw(1, "Bug"), _raw(2, "Bug", "Feature"), _raw(3, "bug", pr=True)],
        "feature": [_raw(2, "Bug", "Feature"), _raw(4, "FEATURE")],
        "documentation": [],
        "question": [_raw(5, "Q", "needs-info")],
    }
    for category, items in pages.items():
        fce._cache_path("o/r", category).write_text(json.dumps(items))
    rows = fce.build()
    assert [(r["number"], r["category"]) for r in rows] == [
        (1, "bug"),
        (4, "feature"),
        (5, "question"),
    ]


def test_external_repositories_are_not_in_the_training_data():
    assert not set(fce.REPOS) & set(fetch_dataset.REPOS)
    assert all(
        set(labels) == {"bug", "feature", "documentation", "question"}
        for labels in fce.REPOS.values()
    )


def test_template_stripping_keeps_the_writing():
    body = (
        "### Describe the bug\nThe app crashes when saving.\n**Version**\n- [x] I searched\n"
        "_No response_"
    )
    cleaned = gen.strip_template(body)
    assert "crashes when saving" in cleaned
    for scaffold in ("Describe the bug", "Version", "I searched", "No response"):
        assert scaffold not in cleaned
