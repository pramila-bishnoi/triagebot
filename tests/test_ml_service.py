"""Tests for the ML decision logic and every fallback path."""

from __future__ import annotations

import pytest

from app.ml import service
from app.ml.model import IssueClassifier


def test_ml_disabled_uses_rules():
    result = service.classify("App crashes with traceback")
    assert result.category_source == "rules"
    assert result.category_score is None
    assert "TRIAGEBOT_ML_ENABLED=0" in result.category_reasons[0]


def test_missing_model_uses_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIAGEBOT_ML_ENABLED", "1")
    monkeypatch.setenv("TRIAGEBOT_MODEL_PATH", str(tmp_path / "nothing"))
    result = service.classify("App crashes with traceback")
    assert result.category_source == "rules"
    assert result.category == "bug"
    assert "no trained model" in result.category_reasons[0]
    assert service.model_status()["available"] is False


def test_corrupt_model_uses_rules(monkeypatch, tmp_path, tiny_model_path):
    import shutil

    for ext in (".npz", ".json"):
        shutil.copy(tiny_model_path.with_suffix(ext), tmp_path / f"m{ext}")
    (tmp_path / "m.npz").write_bytes(b"garbage")
    monkeypatch.setenv("TRIAGEBOT_ML_ENABLED", "1")
    monkeypatch.setenv("TRIAGEBOT_MODEL_PATH", str(tmp_path / "m"))
    result = service.classify("crash traceback")
    assert result.category_source == "rules"
    assert "checksum" in result.category_reasons[0]


def test_confident_prediction_uses_ml(ml_model):
    result = service.classify("crash with traceback when saving")
    assert result.category_source == "ml"
    assert result.category == "bug"
    assert 0.5 <= result.category_score <= 1.0
    assert result.category_reasons[0].startswith("ML model: bug")
    assert result.category_confidence in ("low", "medium", "high")


def test_ml_category_drives_priority(ml_model):
    # Rules would see no category signal; ML says documentation -> priority adjust -1.
    result = service.classify("typo in tutorial docs page")
    assert result.category_source == "ml"
    assert any("category 'documentation' (-1)" in r for r in result.priority_reasons)


def test_low_confidence_falls_back(ml_model):
    result = service.classify("zzzz qqqq")  # unknown words -> near-uniform probabilities
    assert result.category_source == "rules"
    assert "ML was unsure" in result.category_reasons[0]


def test_threshold_is_configurable(ml_model, monkeypatch):
    monkeypatch.setenv("TRIAGEBOT_ML_THRESHOLD", "1.0")
    assert service.classify("crash with traceback").category_source == "rules"
    monkeypatch.setenv("TRIAGEBOT_ML_THRESHOLD", "0")
    assert service.classify("zzzz qqqq").category_source == "ml"


def test_invalid_threshold_uses_default(ml_model, monkeypatch):
    monkeypatch.setenv("TRIAGEBOT_ML_THRESHOLD", "not-a-number")
    assert service.model_status()["threshold"] == 0.5


def test_inference_error_falls_back(ml_model, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("numerical failure")

    monkeypatch.setattr(IssueClassifier, "predict", boom)
    result = service.classify("crash with traceback")
    assert result.category_source == "rules"
    assert result.category == "bug"
    assert "ML prediction failed" in result.category_reasons[0]


def test_pull_requests_use_rules(ml_model):
    result = service.classify("feat: add export", kind="pr")
    assert result.category_source == "rules"
    assert result.category == "feature"
    assert "issues only" in result.category_reasons[0]


@pytest.mark.parametrize(
    ("labels", "category"),
    [
        (["bug"], "bug"),
        (["Enhancement", "good first issue"], "feature"),
        (["docs"], "documentation"),
        (["question"], "question"),
    ],
)
def test_maintainer_label_wins(ml_model, labels, category):
    result = service.classify("crash with traceback", labels=labels)
    assert result.category == category
    assert result.category_source == "github_label"
    assert "maintainer label" in result.category_reasons[0]


def test_conflicting_labels_go_to_ml(ml_model):
    result = service.classify("crash with traceback", labels=["bug", "documentation"])
    assert result.category_source == "ml"


def test_maintainer_label_category():
    assert service.maintainer_label_category(["Bug"]) == ("bug", "Bug")
    assert service.maintainer_label_category(["help wanted"]) is None
    assert service.maintainer_label_category(["bug", "question"]) is None
    assert service.maintainer_label_category([]) is None


@pytest.mark.parametrize(
    ("p", "label"), [(0.95, "high"), (0.8, "high"), (0.7, "medium"), (0.6, "medium"), (0.55, "low")]
)
def test_confidence_label(p, label):
    assert service.confidence_label(p) == label


def test_model_status_reports_metadata(ml_model):
    status = service.model_status()
    assert status["available"] is True
    assert status["classes"] == ["bug", "documentation", "feature", "question"]
    assert status["test_macro_f1"] == 1.0  # from the tiny model's fixture metadata


def test_retrained_model_is_picked_up(ml_model, tmp_path, monkeypatch):
    from tests.ml_fixtures import train_tiny_model

    base = tmp_path / "fresh"
    train_tiny_model(base)
    monkeypatch.setenv("TRIAGEBOT_MODEL_PATH", str(base))
    assert service.load_model()[0] is not None
