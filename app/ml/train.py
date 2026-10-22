"""Training helpers: fit, tune, and evaluate the classifier.

Kept separate from the CLI (``scripts/train_classifier.py``) so the logic can
be unit-tested on small inputs.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    precision_recall_fscore_support,
)
from sklearn.model_selection import GridSearchCV, StratifiedKFold
from sklearn.pipeline import Pipeline

from app.ml.model import IssueClassifier

LABELS = ["bug", "feature", "documentation", "question"]

VECTORIZER_PARAMS: dict[str, Any] = {
    "ngram_range": (1, 2),  # single words and two-word phrases
    "min_df": 2,  # ignore terms seen in only one issue
    "max_df": 0.9,  # ignore terms in >90% of issues
    "max_features": 50_000,
    "sublinear_tf": True,  # 1 + log(tf): long bodies don't dominate
    "strip_accents": "unicode",
}
C_GRID = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)


def make_pipeline(C: float = 1.0, vectorizer_params: dict | None = None, seed: int = 0) -> Pipeline:
    return Pipeline(
        [
            ("tfidf", TfidfVectorizer(**(vectorizer_params or VECTORIZER_PARAMS))),
            (
                "clf",
                LogisticRegression(C=C, class_weight="balanced", max_iter=3000, random_state=seed),
            ),
        ]
    )


def fit(
    texts: list[str],
    labels: list[str],
    *,
    C: float = 1.0,
    vectorizer_params: dict | None = None,
    seed: int = 0,
    metadata: dict | None = None,
) -> IssueClassifier:
    """Fit TF-IDF + logistic regression on preprocessed ``texts``."""
    if len(set(labels)) < 2:
        raise ValueError("need at least two classes to train")
    pipeline = make_pipeline(C, vectorizer_params, seed).fit(texts, labels)
    clf: LogisticRegression = pipeline.named_steps["clf"]
    coef, intercept = clf.coef_, clf.intercept_
    if coef.shape[0] == 1:  # binary LR stores one row; expand to one row per class
        coef = np.vstack([-coef[0] / 2, coef[0] / 2])
        intercept = np.array([-intercept[0] / 2, intercept[0] / 2])
    return IssueClassifier(
        pipeline.named_steps["tfidf"],
        coef,
        intercept,
        [str(c) for c in clf.classes_],
        metadata,
    )


def tune_c(
    texts: list[str],
    labels: list[str],
    *,
    grid: tuple[float, ...] = C_GRID,
    folds: int = 5,
    seed: int = 0,
    vectorizer_params: dict | None = None,
) -> tuple[float, dict[float, float]]:
    """Pick C by stratified k-fold CV (macro-F1) on the *training* data only."""
    search = GridSearchCV(
        make_pipeline(vectorizer_params=vectorizer_params, seed=seed),
        {"clf__C": list(grid)},
        scoring="f1_macro",
        cv=StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed),
        n_jobs=1,
    )
    search.fit(texts, labels)
    scores = {
        float(c): float(s)
        for c, s in zip(
            search.cv_results_["param_clf__C"], search.cv_results_["mean_test_score"], strict=True
        )
    }
    return float(search.best_params_["clf__C"]), scores


def evaluate(
    y_true: list[str], y_pred: list[str], labels: list[str] | None = None
) -> dict[str, Any]:
    """Accuracy, macro precision / recall / F1, per-class scores, confusion matrix."""
    labels = labels or LABELS
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=labels, zero_division=0
    )
    macro_p, macro_r, macro_f1, _ = precision_recall_fscore_support(
        y_true, y_pred, labels=labels, average="macro", zero_division=0
    )
    return {
        "n": len(y_true),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "macro_precision": float(macro_p),
        "macro_recall": float(macro_r),
        "macro_f1": float(macro_f1),
        "per_class": {
            label: {"precision": float(p), "recall": float(r), "f1": float(f), "support": int(s)}
            for label, p, r, f, s in zip(labels, precision, recall, f1, support, strict=True)
        },
        "labels": labels,
        # rows = true class, columns = predicted class
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=labels).tolist(),
    }


def coverage_at_thresholds(
    proba: np.ndarray,
    classes: list[str],
    y_true: list[str],
    thresholds: tuple[float, ...] = (0.4, 0.5, 0.6, 0.7, 0.8),
) -> list[dict[str, float]]:
    """For each threshold: share of items the model would decide, and its accuracy on them."""
    best = proba.argmax(axis=1)
    conf = proba.max(axis=1)
    predicted = np.array([classes[i] for i in best])
    truth = np.array(y_true)
    rows = []
    for t in thresholds:
        mask = conf >= t
        covered = int(mask.sum())
        rows.append(
            {
                "threshold": t,
                "coverage": covered / len(truth) if len(truth) else 0.0,
                "accuracy": float((predicted[mask] == truth[mask]).mean()) if covered else 0.0,
            }
        )
    return rows
