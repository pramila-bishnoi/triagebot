"""Train and evaluate the TF-IDF + logistic-regression issue classifier.

Steps (all seeded, so re-running on the same data gives the same model):

1. Load ``data/issues.jsonl`` (created by ``python -m scripts.fetch_dataset``).
2. Preprocess and drop duplicate texts *before* splitting, so an identical
   issue can never appear in both train and test.
3. Stratified 80/20 train / test split.
4. Choose C by 5-fold cross-validation on the training split only.
5. Fit on the training split; evaluate once on the untouched test split.
6. Baseline: score the Phase 1 rule engine on the same test split (without
   GitHub labels, which would leak the answer).
7. Cross-repository check: leave-one-repository-out — train on nine repos,
   test on the tenth — to estimate performance on repositories never seen.
8. Save the model trained in step 5 (the one whose test scores are reported)
   to ``models/issue_classifier.{npz,json}`` and write ``models/EVALUATION.md``.

Usage:
    python -m scripts.train_classifier
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import sklearn
from sklearn.model_selection import train_test_split

from app.ml.preprocess import build_text
from app.ml.train import (
    C_GRID,
    VECTORIZER_PARAMS,
    coverage_at_thresholds,
    evaluate,
    fit,
    tune_c,
)
from app.triage import classify_item

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "data" / "issues.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "dataset_manifest.csv"
DEFAULT_MODEL = ROOT / "models" / "issue_classifier"
DEFAULT_REPORT = ROOT / "models" / "EVALUATION.md"


def load_dataset(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"{path} not found — run `python -m scripts.fetch_dataset` first.")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    for row in rows:
        row["text"] = build_text(row["title"], row["body"])
    return rows


def dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    """Drop rows whose preprocessed text was already seen (keeps the first)."""
    seen: set[str] = set()
    kept = []
    for row in rows:
        if not row["text"] or row["text"] in seen:
            continue
        seen.add(row["text"])
        kept.append(row)
    return kept, len(rows) - len(kept)


def rules_predict(rows: list[dict]) -> list[str]:
    return [classify_item(r["title"], r["body"], kind="issue").category for r in rows]


def leave_one_repo_out(
    rows: list[dict], C: float, seed: int, vectorizer_params: dict
) -> list[dict]:
    results = []
    for repo in sorted({r["repo"] for r in rows}):
        train = [r for r in rows if r["repo"] != repo]
        test = [r for r in rows if r["repo"] == repo]
        model = fit(
            [r["text"] for r in train],
            [r["category"] for r in train],
            C=C,
            seed=seed,
            vectorizer_params=vectorizer_params,
        )
        y_true = [r["category"] for r in test]
        ml = evaluate(y_true, model.predict_texts([r["text"] for r in test]))
        rules = evaluate(y_true, rules_predict(test))
        results.append(
            {
                "repo": repo,
                "n": len(test),
                "ml_accuracy": ml["accuracy"],
                "ml_macro_f1": ml["macro_f1"],
                "rules_macro_f1": rules["macro_f1"],
            }
        )
    return results


def run_training(
    dataset: Path,
    model_base: Path,
    report: Path,
    *,
    seed: int = 42,
    test_size: float = 0.2,
    folds: int = 5,
    grid: tuple[float, ...] = C_GRID,
    vectorizer_params: dict | None = None,
    manifest: Path | None = None,
    cross_repo: bool = True,
) -> dict:
    vectorizer_params = vectorizer_params or VECTORIZER_PARAMS
    rows, duplicates = dedupe(load_dataset(dataset))
    labels = [r["category"] for r in rows]
    train_rows, test_rows = train_test_split(
        rows, test_size=test_size, stratify=labels, random_state=seed
    )
    x_train = [r["text"] for r in train_rows]
    y_train = [r["category"] for r in train_rows]
    x_test = [r["text"] for r in test_rows]
    y_test = [r["category"] for r in test_rows]

    print(
        f"{len(rows)} issues after removing {duplicates} duplicates; "
        f"train {len(train_rows)}, test {len(test_rows)}"
    )
    best_c, cv_scores = tune_c(
        x_train, y_train, grid=grid, folds=folds, seed=seed, vectorizer_params=vectorizer_params
    )
    print(f"best C = {best_c} (CV macro-F1 {cv_scores[best_c]:.3f})")

    model = fit(x_train, y_train, C=best_c, seed=seed, vectorizer_params=vectorizer_params)
    proba = model.predict_proba_texts(x_test)
    y_pred = [model.classes[i] for i in proba.argmax(axis=1)]
    test_metrics = evaluate(y_test, y_pred)
    rules_metrics = evaluate(y_test, rules_predict(test_rows))
    coverage = coverage_at_thresholds(proba, model.classes, y_test)
    print(
        f"test accuracy {test_metrics['accuracy']:.3f}, macro-F1 {test_metrics['macro_f1']:.3f}"
        f" (rules baseline macro-F1 {rules_metrics['macro_f1']:.3f})"
    )

    loro = leave_one_repo_out(rows, best_c, seed, vectorizer_params) if cross_repo else []
    if loro:
        print(
            f"leave-one-repo-out mean macro-F1 " f"{np.mean([r['ml_macro_f1'] for r in loro]):.3f}"
        )

    manifest_sha = (
        hashlib.sha256(manifest.read_bytes()).hexdigest()
        if manifest and manifest.exists()
        else None
    )
    metadata = {
        "model": "TF-IDF (1-2 grams) + multinomial logistic regression",
        "trained_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "versions": {
            "python": platform.python_version(),
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
        },
        "dataset": {
            "source": "maintainer-labelled public GitHub issues (see scripts/fetch_dataset.py)",
            "manifest_sha256": manifest_sha,
            "issues": len(rows),
            "duplicates_removed": duplicates,
            "class_counts": dict(Counter(labels)),
            "repos": dict(Counter(r["repo"] for r in rows)),
        },
        "training": {
            "seed": seed,
            "test_size": test_size,
            "train": len(train_rows),
            "test": len(test_rows),
            "cv_folds": folds,
            "C": best_c,
            "cv_macro_f1_by_C": {str(k): v for k, v in cv_scores.items()},
            "vectorizer_params": {
                k: list(v) if isinstance(v, tuple) else v for k, v in vectorizer_params.items()
            },
            "class_weight": "balanced",
        },
        "evaluation": {
            "test": test_metrics,
            "rules_baseline_test": rules_metrics,
            "coverage_at_thresholds": coverage,
            "leave_one_repo_out": loro,
            "leave_one_repo_out_mean_macro_f1": (
                float(np.mean([r["ml_macro_f1"] for r in loro])) if loro else None
            ),
            "leave_one_repo_out_mean_rules_macro_f1": (
                float(np.mean([r["rules_macro_f1"] for r in loro])) if loro else None
            ),
        },
    }
    model.metadata = metadata
    model.save(model_base)
    report.write_text(render_report(metadata), encoding="utf-8")
    print(f"saved {model_base}.npz / .json and {report}")
    return metadata


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def render_report(meta: dict) -> str:
    ev, tr, ds = meta["evaluation"], meta["training"], meta["dataset"]
    test, rules = ev["test"], ev["rules_baseline_test"]
    per_class = _table(
        ["class", "precision", "recall", "F1", "support", "rules F1"],
        [
            [
                c,
                f"{m['precision']:.3f}",
                f"{m['recall']:.3f}",
                f"{m['f1']:.3f}",
                str(m["support"]),
                f"{rules['per_class'][c]['f1']:.3f}",
            ]
            for c, m in test["per_class"].items()
        ],
    )
    cm = _table(
        ["true \\ predicted", *test["labels"]],
        [
            [t, *map(str, row)]
            for t, row in zip(test["labels"], test["confusion_matrix"], strict=True)
        ],
    )
    cov = _table(
        ["min. probability", "share of issues decided by ML", "accuracy on those"],
        [
            [f"{r['threshold']:.1f}", f"{r['coverage']:.1%}", f"{r['accuracy']:.3f}"]
            for r in ev["coverage_at_thresholds"]
        ],
    )
    parts = [
        "# Issue classifier — evaluation report",
        "",
        "_Generated by `python -m scripts.train_classifier`; do not edit by hand._",
        "",
        f"- Model: {meta['model']}, C = {tr['C']} (chosen by {tr['cv_folds']}-fold CV on "
        f"the training split), class_weight = balanced",
        f"- Trained: {meta['trained_at']} · scikit-learn {meta['versions']['scikit_learn']}",
        f"- Data: {ds['issues']} issues from {len(ds['repos'])} repositories "
        f"({ds['duplicates_removed']} duplicates removed); "
        f"train {tr['train']} / test {tr['test']} (stratified, seed {tr['seed']})",
        "",
        "## Held-out test split",
        "",
        _table(
            ["metric", "ML model", "rule engine (Phase 1)"],
            [
                ["accuracy", f"{test['accuracy']:.3f}", f"{rules['accuracy']:.3f}"],
                [
                    "macro precision",
                    f"{test['macro_precision']:.3f}",
                    f"{rules['macro_precision']:.3f}",
                ],
                ["macro recall", f"{test['macro_recall']:.3f}", f"{rules['macro_recall']:.3f}"],
                ["macro F1", f"{test['macro_f1']:.3f}", f"{rules['macro_f1']:.3f}"],
            ],
        ),
        "",
        per_class,
        "",
        "### Confusion matrix (ML model, test split)",
        "",
        cm,
        "",
        "### Confidence vs. accuracy (test split)",
        "",
        cov,
        "",
    ]
    if ev["leave_one_repo_out"]:
        parts += [
            "## Unseen repositories (leave-one-repository-out)",
            "",
            "Each row: trained on the other repositories, tested on this one.",
            "",
            _table(
                ["held-out repo", "issues", "ML accuracy", "ML macro F1", "rules macro F1"],
                [
                    [
                        r["repo"],
                        str(r["n"]),
                        f"{r['ml_accuracy']:.3f}",
                        f"{r['ml_macro_f1']:.3f}",
                        f"{r['rules_macro_f1']:.3f}",
                    ]
                    for r in ev["leave_one_repo_out"]
                ],
            ),
            "",
            f"Mean macro F1 — ML: {ev['leave_one_repo_out_mean_macro_f1']:.3f}, "
            f"rules: {ev['leave_one_repo_out_mean_rules_macro_f1']:.3f}",
            "",
        ]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the issue category classifier.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out", type=Path, default=DEFAULT_MODEL, help="model base path")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--no-cross-repo",
        action="store_true",
        help="skip the slower leave-one-repository-out evaluation",
    )
    args = parser.parse_args()
    run_training(
        args.dataset,
        args.out,
        args.report,
        seed=args.seed,
        manifest=DEFAULT_MANIFEST,
        cross_repo=not args.no_cross_repo,
    )


if __name__ == "__main__":
    main()
