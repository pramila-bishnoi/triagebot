"""A tiny, hand-written corpus for *unit tests only*.

These sentences are synthetic test fixtures used to exercise the training and
inference code quickly and deterministically. They are not training data for
the shipped model and say nothing about its accuracy — see
``models/EVALUATION.md`` for real evaluation results.
"""

from __future__ import annotations

from pathlib import Path

from app.ml.model import IssueClassifier
from app.ml.preprocess import build_text
from app.ml.train import fit

TINY_VECTORIZER = {"ngram_range": (1, 1), "min_df": 1, "sublinear_tf": True}

TINY_CORPUS: dict[str, list[str]] = {
    "bug": [
        "crash with traceback when saving file",
        "exception raised on startup traceback",
        "segfault crash after upgrade",
        "error traceback when parsing input",
        "app crashes with null pointer exception",
        "regression crash in latest release traceback",
        "unexpected exception thrown and crash",
        "traceback error when clicking save",
    ],
    "feature": [
        "please add support for dark mode",
        "feature request add export option",
        "would be nice to add a plugin api",
        "add support for yaml configuration",
        "proposal add new command option",
        "add option to customise keyboard shortcuts",
        "support exporting results to csv please add",
        "new feature add filtering option",
    ],
    "documentation": [
        "docs typo in readme installation section",
        "documentation is missing an example",
        "readme link broken in docs",
        "improve documentation for configuration docs",
        "typo in tutorial docs page",
        "docs example outdated in readme",
        "clarify documentation wording in guide docs",
        "missing docstring documentation for function",
    ],
    "question": [
        "how do i configure the proxy question",
        "question how can i run this on windows",
        "is it possible to use this with docker question",
        "how do i install on mac question",
        "question about how to use the api",
        "how can i change the default port question",
        "what is the recommended way question",
        "how do i enable verbose logging question",
    ],
}


def corpus() -> tuple[list[str], list[str]]:
    texts, labels = [], []
    for label, sentences in TINY_CORPUS.items():
        for sentence in sentences:
            texts.append(build_text(sentence, ""))
            labels.append(label)
    return texts, labels


def train_tiny_model(base: Path) -> IssueClassifier:
    texts, labels = corpus()
    model = fit(texts, labels, C=10.0, vectorizer_params=TINY_VECTORIZER,
                metadata={"model": "tiny test model", "trained_at": "test",
                          "evaluation": {"test": {"accuracy": 1.0, "macro_f1": 1.0}}})
    model.save(base)
    return model
