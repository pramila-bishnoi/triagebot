"""Runtime category decision: maintainer label → ML model → rule-based fallback.

Decision order for each item:

1. **Pull requests** use the rule engine — the model was trained on issues only.
2. **No usable model** (disabled, missing, invalid) — the Phase 1 rule engine,
   exactly as before (it already weighs GitHub labels heavily).
3. **Maintainer label** — if the item already carries exactly one category
   label (e.g. ``bug``, ``enhancement``), that is used as-is.
4. **ML model** — used when its top probability is at least
   ``TRIAGEBOT_ML_THRESHOLD`` (default 0.5).
5. **Rule engine** — for low-confidence predictions or any inference error.

Every fallback adds a note to the explanation saying why.

Priority is always computed by :mod:`app.priority` for the final category
(a maintainer's explicit priority label is kept as-is).
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from functools import lru_cache
from typing import Any

from app.config import get_settings
from app.ml.model import IssueClassifier, ModelError
from app.triage import CATEGORY_LABELS, Classification, classify_item

log = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _load(path: str, _mtime: float) -> tuple[IssueClassifier | None, str | None]:
    try:
        return IssueClassifier.load(path), None
    except ModelError as exc:
        log.warning("ML model unavailable: %s", exc)
        return None, str(exc)
    except Exception as exc:  # never let a bad model file crash the app
        log.exception("unexpected error loading ML model")
        return None, f"could not load model: {exc}"


def load_model() -> tuple[IssueClassifier | None, str | None]:
    """Return ``(model, None)`` or ``(None, reason)``. Reloads if the file changes."""
    settings = get_settings()
    if not settings.ml_enabled:
        return None, "ML disabled (TRIAGEBOT_ML_ENABLED=0)"
    meta = f"{settings.model_path}.json"
    try:
        mtime = os.path.getmtime(meta)
    except OSError:
        return None, f"no trained model at {settings.model_path}.npz / .json"
    return _load(settings.model_path, mtime)


def clear_cache() -> None:
    _load.cache_clear()


def model_status() -> dict[str, Any]:
    """Summary for the ``/model`` endpoint and the dashboard."""
    model, reason = load_model()
    settings = get_settings()
    if model is None:
        return {"available": False, "reason": reason, "threshold": settings.ml_threshold}
    meta = model.metadata
    evaluation = meta.get("evaluation", {})
    test = evaluation.get("test", {})
    return {
        "available": True,
        "reason": None,
        "threshold": settings.ml_threshold,
        "model": meta.get("model"),
        "classes": model.classes,
        "trained_at": meta.get("trained_at"),
        "train_size": meta.get("training", {}).get("train"),
        "test_size": meta.get("training", {}).get("test"),
        "test_accuracy": test.get("accuracy"),
        "test_macro_f1": test.get("macro_f1"),
        "rules_baseline_macro_f1": evaluation.get("rules_baseline_test", {}).get("macro_f1"),
        "unseen_repo_macro_f1": evaluation.get("leave_one_repo_out_mean_macro_f1"),
    }


def maintainer_label_category(labels: list[str]) -> tuple[str, str] | None:
    """``(category, label)`` if the labels name exactly one category, else ``None``."""
    found: dict[str, str] = {}
    for label in labels:
        name = label.strip().lower()
        for category, names in CATEGORY_LABELS.items():
            if name in names:
                found.setdefault(category, label)
    if len(found) == 1:
        return next(iter(found.items()))
    return None


def confidence_label(probability: float) -> str:
    if probability >= 0.8:
        return "high"
    if probability >= 0.6:
        return "medium"
    return "low"


def _with_note(result: Classification, note: str) -> Classification:
    return replace(result, category_reasons=[note, *result.category_reasons])


def classify(
    title: str,
    body: str | None = None,
    *,
    kind: str = "issue",
    labels: list[str] | None = None,
    reactions: int = 0,
    comments: int = 0,
    is_draft: bool = False,
) -> Classification:
    """Category (with source and score) + priority for an issue or PR."""
    labels = labels or []
    common = {
        "kind": kind,
        "labels": labels,
        "reactions": reactions,
        "comments": comments,
        "is_draft": is_draft,
    }

    if kind == "pr":
        return _with_note(
            classify_item(title, body, **common),
            "rule engine used: the ML model is trained on issues only",
        )

    model, reason = load_model()
    if model is None:
        return _with_note(classify_item(title, body, **common), f"rule engine used: {reason}")

    label_hit = maintainer_label_category(labels)
    if label_hit:
        category, label = label_hit
        result = classify_item(title, body, **common, category_override=category)
        return replace(
            result,
            category_source="github_label",
            category_confidence="high",
            category_reasons=[f"maintainer label '{label}' -> {category}"],
        )

    try:
        prediction = model.predict(title, body)
    except Exception:  # fall back rather than fail an import
        log.exception("ML prediction failed")
        return _with_note(
            classify_item(title, body, **common), "rule engine used: ML prediction failed"
        )

    threshold = get_settings().ml_threshold
    if prediction.confidence < threshold:
        return _with_note(
            classify_item(title, body, **common),
            f"rule engine used: ML was unsure ({prediction.category} "
            f"{prediction.confidence:.0%} < {threshold:.0%} threshold)",
        )

    result = classify_item(title, body, **common, category_override=prediction.category)
    return replace(
        result,
        category_source="ml",
        category_score=round(prediction.confidence, 4),
        category_confidence=confidence_label(prediction.confidence),
        category_reasons=prediction.explanation(),
    )
