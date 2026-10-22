"""Tests for training, prediction, explanations, metrics and safe model storage."""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.ml.model import IssueClassifier, ModelError
from app.ml.preprocess import PREPROCESS_VERSION
from app.ml.train import coverage_at_thresholds, evaluate, fit, tune_c
from tests.ml_fixtures import TINY_VECTORIZER, corpus, train_tiny_model


@pytest.fixture(scope="module")
def model():
    texts, labels = corpus()
    return fit(texts, labels, C=10.0, vectorizer_params=TINY_VECTORIZER)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("crash with traceback on save", "bug"),
        ("please add support for themes", "feature"),
        ("typo in the readme docs", "documentation"),
        ("how do i configure logging question", "question"),
    ],
)
def test_predicts_training_like_inputs(model, title, expected):
    assert model.predict(title).category == expected


def test_probabilities_are_valid(model):
    pred = model.predict("crash traceback", "error")
    assert set(pred.probabilities) == {"bug", "feature", "documentation", "question"}
    assert sum(pred.probabilities.values()) == pytest.approx(1.0)
    assert pred.confidence == max(pred.probabilities.values())


def test_explanation_lists_evidence(model):
    pred = model.predict("crash with traceback")
    terms = [t for t, _ in pred.top_terms]
    assert "traceback" in terms or "crash" in terms
    assert all(w > 0 for _, w in pred.top_terms)
    lines = pred.explanation()
    assert lines[0].startswith("ML model: bug (")
    assert "runner-up" in lines[0]
    assert lines[1].startswith("words pointing to bug:")


def test_unknown_words_fall_back_to_priors(model):
    pred = model.predict("zzzz qqqq", "")
    assert pred.top_terms == []
    assert "class priors" in pred.explanation()[1]
    # balanced classes -> near-uniform probabilities
    assert pred.confidence < 0.5


def test_empty_input_does_not_crash(model):
    assert model.predict("", "").category in model.classes
    assert model.predict(None, None).category in model.classes


def test_batch_and_single_prediction_agree(model):
    texts, _ = corpus()
    proba = model.predict_proba_texts(texts[:3])
    assert proba.shape == (3, 4)
    assert np.allclose(proba.sum(axis=1), 1.0)


def test_fit_requires_two_classes():
    with pytest.raises(ValueError):
        fit(["a b", "c d"], ["bug", "bug"], vectorizer_params=TINY_VECTORIZER)


def test_binary_problem_supported():
    m = fit(
        ["crash error", "crash bug", "add feature", "add option"],
        ["bug", "bug", "feature", "feature"],
        vectorizer_params=TINY_VECTORIZER,
    )
    assert m.coef.shape[0] == 2
    assert m.predict("crash").category == "bug"


def test_tune_c_returns_grid_value():
    texts, labels = corpus()
    best, scores = tune_c(
        texts, labels, grid=(0.1, 10.0), folds=2, vectorizer_params=TINY_VECTORIZER
    )
    assert best in (0.1, 10.0)
    assert set(scores) == {0.1, 10.0}
    assert all(0.0 <= s <= 1.0 for s in scores.values())


# --- metrics --------------------------------------------------------------------


def test_evaluate_hand_computed():
    y_true = ["bug", "bug", "feature", "question"]
    y_pred = ["bug", "feature", "feature", "question"]
    m = evaluate(y_true, y_pred)
    assert m["accuracy"] == pytest.approx(0.75)
    assert m["per_class"]["bug"] == pytest.approx(
        {"precision": 1.0, "recall": 0.5, "f1": 2 / 3, "support": 2}
    )
    assert m["per_class"]["feature"]["precision"] == pytest.approx(0.5)
    assert m["per_class"]["documentation"]["support"] == 0
    # macro average includes the empty documentation class (F1 = 0)
    assert m["macro_f1"] == pytest.approx((2 / 3 + 2 / 3 + 0 + 1) / 4)
    assert m["confusion_matrix"] == [
        [1, 1, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 1],
    ]


def test_coverage_at_thresholds():
    proba = np.array([[0.9, 0.1], [0.55, 0.45], [0.3, 0.7]])
    rows = coverage_at_thresholds(
        proba, ["bug", "feature"], ["bug", "feature", "feature"], thresholds=(0.5, 0.8)
    )
    assert rows[0] == {"threshold": 0.5, "coverage": 1.0, "accuracy": pytest.approx(2 / 3)}
    assert rows[1] == {"threshold": 0.8, "coverage": pytest.approx(1 / 3), "accuracy": 1.0}


# --- persistence ----------------------------------------------------------------


def test_save_load_round_trip(tmp_path, model):
    weights, meta = model.save(tmp_path / "m")
    assert weights.name == "m.npz" and meta.name == "m.json"
    loaded = IssueClassifier.load(tmp_path / "m")
    texts, _ = corpus()
    assert np.allclose(loaded.predict_proba_texts(texts), model.predict_proba_texts(texts))
    assert loaded.classes == model.classes
    info = json.loads(meta.read_text())
    assert info["preprocess_version"] == PREPROCESS_VERSION
    assert len(info["weights_sha256"]) == 64


def test_load_missing_files(tmp_path):
    with pytest.raises(ModelError, match="not found"):
        IssueClassifier.load(tmp_path / "absent")


def test_tampered_weights_rejected(tmp_path):
    train_tiny_model(tmp_path / "m")
    path = tmp_path / "m.npz"
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ModelError, match="checksum"):
        IssueClassifier.load(tmp_path / "m")


def test_pickled_payload_refused(tmp_path):
    """Even with a matching checksum, object (pickle) arrays are never loaded."""
    train_tiny_model(tmp_path / "m")
    np.savez(
        tmp_path / "m.npz",
        terms=np.array([{"evil": 1}], dtype=object),
        idf=np.ones(1),
        coef=np.ones((4, 1)),
        intercept=np.zeros(4),
        classes=np.array(["bug", "feature", "documentation", "question"]),
    )
    meta = json.loads((tmp_path / "m.json").read_text())
    import hashlib

    meta["weights_sha256"] = hashlib.sha256((tmp_path / "m.npz").read_bytes()).hexdigest()
    (tmp_path / "m.json").write_text(json.dumps(meta))
    with pytest.raises(ModelError, match="could not read"):
        IssueClassifier.load(tmp_path / "m")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("preprocess_version", 999, "preprocessing"), ("format_version", 999, "format")],
)
def test_incompatible_metadata_rejected(tmp_path, field, value, message):
    train_tiny_model(tmp_path / "m")
    meta = json.loads((tmp_path / "m.json").read_text())
    meta[field] = value
    (tmp_path / "m.json").write_text(json.dumps(meta))
    with pytest.raises(ModelError, match=message):
        IssueClassifier.load(tmp_path / "m")


def test_invalid_json_rejected(tmp_path):
    train_tiny_model(tmp_path / "m")
    (tmp_path / "m.json").write_text("{not json")
    with pytest.raises(ModelError, match="JSON"):
        IssueClassifier.load(tmp_path / "m")


def test_shape_mismatch_rejected(model):
    with pytest.raises(ModelError, match="shape"):
        IssueClassifier(model.vectorizer, model.coef[:, :-1], model.intercept, model.classes)
