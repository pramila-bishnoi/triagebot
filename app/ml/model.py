"""The TF-IDF + logistic-regression issue classifier.

Saved models are two files sharing a base path:

* ``<base>.npz`` — vocabulary, IDF weights, coefficients, intercepts and class
  names as plain numeric / string arrays. Loaded with ``allow_pickle=False``,
  so a tampered file cannot execute code (unlike pickle / joblib).
* ``<base>.json`` — metadata: vectorizer settings, preprocessing version,
  training details, evaluation metrics, and the SHA-256 of the ``.npz``,
  which is verified before loading.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from app.ml.preprocess import PREPROCESS_VERSION, build_text

FORMAT_VERSION = 1

# Vectorizer settings that affect inference (and therefore must be saved).
INFERENCE_PARAMS = (
    "lowercase", "strip_accents", "token_pattern", "ngram_range",
    "sublinear_tf", "norm", "use_idf", "smooth_idf",
)


class ModelError(Exception):
    """The model file is missing, corrupt, or incompatible."""


@dataclass(frozen=True)
class Prediction:
    category: str
    confidence: float                     # probability of ``category``
    probabilities: dict[str, float]       # all classes, sums to 1
    top_terms: list[tuple[str, float]] = field(default_factory=list)

    def explanation(self) -> list[str]:
        ranked = sorted(self.probabilities.items(), key=lambda kv: -kv[1])
        lines = [f"ML model: {self.category} ({self.confidence:.0%})"]
        if len(ranked) > 1:
            runner, prob = ranked[1]
            lines[0] += f", runner-up {runner} ({prob:.0%})"
        if self.top_terms:
            terms = ", ".join(f"'{t}' (+{w:.2f})" for t, w in self.top_terms)
            lines.append(f"words pointing to {self.category}: {terms}")
        else:
            lines.append("no known words in the text; prediction is based on class priors")
        return lines


def _softmax(scores: np.ndarray) -> np.ndarray:
    scores = scores - scores.max(axis=1, keepdims=True)
    exp = np.exp(scores)
    return exp / exp.sum(axis=1, keepdims=True)


class IssueClassifier:
    """Inference wrapper around a fitted vectorizer + multinomial LR weights."""

    def __init__(
        self,
        vectorizer: TfidfVectorizer,
        coef: np.ndarray,
        intercept: np.ndarray,
        classes: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if coef.shape != (len(classes), len(vectorizer.vocabulary_)):
            raise ModelError("coefficient shape does not match vocabulary / classes")
        self.vectorizer = vectorizer
        self.coef = coef
        self.intercept = intercept
        self.classes = list(classes)
        self.metadata = metadata or {}
        # Softmax is unchanged by adding the same vector to every class, so
        # centring makes each weight read as "evidence for this class vs. the
        # average class" — a fairer basis for explanations.
        self._centred = coef - coef.mean(axis=0, keepdims=True)
        self._terms = np.empty(len(vectorizer.vocabulary_), dtype=object)
        for term, index in vectorizer.vocabulary_.items():
            self._terms[index] = term

    # -- inference ------------------------------------------------------------

    def predict_proba_texts(self, texts: list[str]) -> np.ndarray:
        """Class probabilities for already-preprocessed texts (rows follow ``classes``)."""
        features = self.vectorizer.transform(texts)
        return _softmax(np.asarray(features @ self.coef.T) + self.intercept)

    def predict_texts(self, texts: list[str]) -> list[str]:
        proba = self.predict_proba_texts(texts)
        return [self.classes[i] for i in proba.argmax(axis=1)]

    def predict(self, title: str | None, body: str | None = None, top_n: int = 5) -> Prediction:
        text = build_text(title, body)
        features = self.vectorizer.transform([text])
        proba = _softmax(np.asarray(features @ self.coef.T) + self.intercept)[0]
        best = int(proba.argmax())

        row = features.tocsr()
        contributions = row.data * self._centred[best, row.indices]
        order = np.argsort(-contributions)
        top_terms = [
            (str(self._terms[row.indices[i]]), float(contributions[i]))
            for i in order[:top_n]
            if contributions[i] > 0
        ]
        return Prediction(
            category=self.classes[best],
            confidence=float(proba[best]),
            probabilities={c: float(p) for c, p in zip(self.classes, proba, strict=True)},
            top_terms=top_terms,
        )

    # -- persistence ------------------------------------------------------------

    def save(self, base: Path | str) -> tuple[Path, Path]:
        base = Path(base)
        base.parent.mkdir(parents=True, exist_ok=True)
        weights_path, meta_path = base.with_suffix(".npz"), base.with_suffix(".json")
        np.savez_compressed(
            weights_path,
            terms=np.asarray(self._terms.astype(str)),
            idf=self.vectorizer.idf_,
            coef=self.coef,
            intercept=self.intercept,
            classes=np.asarray(self.classes),
        )
        params = self.vectorizer.get_params()
        metadata = {
            **self.metadata,
            "format_version": FORMAT_VERSION,
            "preprocess_version": PREPROCESS_VERSION,
            "vectorizer": {
                key: list(params[key]) if isinstance(params[key], tuple) else params[key]
                for key in INFERENCE_PARAMS
            },
            "classes": self.classes,
            "vocabulary_size": len(self.vectorizer.vocabulary_),
            "weights_file": weights_path.name,
            "weights_sha256": hashlib.sha256(weights_path.read_bytes()).hexdigest(),
        }
        meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        return weights_path, meta_path

    @classmethod
    def load(cls, base: Path | str) -> IssueClassifier:
        base = Path(base)
        weights_path, meta_path = base.with_suffix(".npz"), base.with_suffix(".json")
        if not meta_path.exists() or not weights_path.exists():
            raise ModelError(f"model files not found at {base}.npz / .json")
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ModelError("model metadata is not valid JSON") from exc

        if metadata.get("format_version") != FORMAT_VERSION:
            raise ModelError("unsupported model format; retrain the model")
        if metadata.get("preprocess_version") != PREPROCESS_VERSION:
            raise ModelError("model was trained with different preprocessing; retrain it")
        digest = hashlib.sha256(weights_path.read_bytes()).hexdigest()
        if digest != metadata.get("weights_sha256"):
            raise ModelError("model weights checksum mismatch (file modified or corrupt)")

        try:
            with np.load(weights_path, allow_pickle=False) as data:
                terms = [str(t) for t in data["terms"]]
                idf, coef, intercept = data["idf"], data["coef"], data["intercept"]
                classes = [str(c) for c in data["classes"]]
        except (ValueError, KeyError, OSError) as exc:
            raise ModelError(f"could not read model weights: {exc}") from exc

        params = dict(metadata["vectorizer"])
        params["ngram_range"] = tuple(params["ngram_range"])
        vectorizer = TfidfVectorizer(**params, vocabulary={t: i for i, t in enumerate(terms)})
        if idf.shape != (len(terms),):
            raise ModelError("IDF shape does not match vocabulary")
        vectorizer.idf_ = idf
        return cls(vectorizer, coef, intercept, classes, metadata)
