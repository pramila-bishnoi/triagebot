"""Phase 6: re-assess how well the category classifier generalises to new repositories.

Two stages, kept separate on purpose:

1. **Development** (training data only, ``data/issues.jsonl``):
   leave-one-repository-out (LORO) macro-F1 over the ten training repositories
   for the shipped configuration and for candidate changes motivated by the
   leakage / feature audit (template text and repository-specific tokens among
   the top-weighted features). The candidate and its C are chosen here.
2. **External test** (``data/category_external.jsonl``, eight repositories that
   played no part in training or in any decision): the shipped model and the
   candidate — trained on the *same* 2,964-issue training split — are scored
   once, with a paired bootstrap of the difference. Also reported: per class,
   per repository, the rules baseline, and the app's actual decision rule
   (ML when its probability is at least the threshold, otherwise the rules)
   at several thresholds.

Decision rule, fixed before the external set was scored: replace the shipped
model only if the candidate is better on LORO (mean gain >= 0.01 and better on
at least 7 of 10 repositories) **and** better on the external set by >= 0.02
macro-F1 with a 95% bootstrap interval above 0. Otherwise keep the shipped model.

Usage:
    python -m scripts.evaluate_category_generalization --out docs/CATEGORY_GENERALIZATION.md
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path

import numpy as np
from sklearn.model_selection import train_test_split

from app.ml.model import IssueClassifier
from app.ml.preprocess import build_text
from app.ml.train import VECTORIZER_PARAMS, evaluate, fit
from app.triage import classify_item
from scripts.train_classifier import DEFAULT_DATASET, DEFAULT_MODEL, dedupe, load_dataset

ROOT = Path(__file__).resolve().parent.parent
EXTERNAL = ROOT / "data" / "category_external.jsonl"
LABELS = ["bug", "feature", "documentation", "question"]
C_VALUES = (1.0, 2.0, 4.0)
THRESHOLDS = (0.4, 0.5, 0.6, 0.7, 0.8)

# Candidate change: drop issue-template scaffolding (Markdown headings, bold
# "field" lines, checklists, "_No response_") from the body before vectorising.
_HEAD = re.compile(r"^\s{0,3}#{1,6}\s.*$", re.M)
_BOLD_LINE = re.compile(r"^\s*\*\*[^*\n]{1,80}\*\*\s*:?\s*$", re.M)
_CHECKBOX = re.compile(r"^\s*[-*+]\s*\[[ xX]\].*$", re.M)
_NO_RESPONSE = re.compile(r"_no response_", re.I)


def strip_template(body: str | None) -> str:
    body = body or ""
    for pattern in (_HEAD, _BOLD_LINE, _CHECKBOX, _NO_RESPONSE):
        body = pattern.sub(" ", body)
    return body


VARIANTS = {
    "shipped preprocessing": lambda r: build_text(r["title"], r["body"]),
    "template text removed": lambda r: build_text(r["title"], strip_template(r["body"])),
}


def audit(
    rows: list[dict], train_rows: list[dict], test_rows: list[dict], model: IssueClassifier
) -> list[str]:
    """Leakage and feature checks behind the candidate change (reported, not tuned)."""
    from sklearn.feature_extraction.text import TfidfVectorizer

    vec = TfidfVectorizer(sublinear_tf=True).fit([r["text"] for r in rows])
    a = vec.transform([r["text"] for r in test_rows])
    b = vec.transform([r["text"] for r in train_rows])
    nearest = (a @ b.T).max(axis=1).toarray().ravel()
    near = {t: int((nearest >= t).sum()) for t in (0.95, 0.9, 0.8)}
    names = {part.lower() for r in rows for part in r["repo"].split("/")}
    lines = [
        f"- Exact duplicate texts are removed before the split. Near-duplicates across "
        f"the split (TF-IDF cosine to the closest training issue): "
        f"{near[0.95]} test issues at ≥ 0.95, {near[0.9]} at ≥ 0.9, {near[0.8]} at ≥ 0.8 "
        f"(of {len(test_rows)}).",
        "- Repository-specific and template terms among each class's 200 "
        "highest-weighted features (repository or owner names in a term):",
    ]
    for ci, cls in enumerate(model.classes):
        top = [str(model._terms[k]) for k in np.argsort(-model._centred[ci])[:200]]
        hits = [t for t in top if set(t.split()) & names]
        lines.append(f"  - {cls}: {len(hits)} — e.g. {', '.join(hits[:6]) or 'none'}")
    counts = Counter((r["repo"], r["category"]) for r in rows)
    smallest = sorted(counts.items(), key=lambda kv: kv[1])[:3]
    lines.append(
        "- Class balance: up to 100 per (repository, class); smallest cells: "
        + ", ".join(f"{repo} {cls} {n}" for (repo, cls), n in smallest)
        + "."
    )
    return lines


def loro(rows: list[dict], texts: list[str], C: float) -> dict[str, float]:
    out = {}
    for repo in sorted({r["repo"] for r in rows}):
        train = [i for i, r in enumerate(rows) if r["repo"] != repo]
        test = [i for i, r in enumerate(rows) if r["repo"] == repo]
        model = fit(
            [texts[i] for i in train],
            [rows[i]["category"] for i in train],
            C=C,
            seed=42,
            vectorizer_params=VECTORIZER_PARAMS,
        )
        pred = model.predict_texts([texts[i] for i in test])
        out[repo] = evaluate([rows[i]["category"] for i in test], pred)["macro_f1"]
    return out


def bootstrap_diff(
    gold: list[str], a: list[str], b: list[str], resamples: int = 2000, seed: int = 0
) -> tuple[float, float, float]:
    """Paired 95% interval of macro-F1(a) − macro-F1(b), resampling issues."""
    rng = random.Random(seed)
    n = len(gold)

    def diff(idx: list[int]) -> float:
        g = [gold[i] for i in idx]
        return (
            evaluate(g, [a[i] for i in idx])["macro_f1"]
            - evaluate(g, [b[i] for i in idx])["macro_f1"]
        )

    point = diff(list(range(n)))
    values = sorted(diff([rng.randrange(n) for _ in range(n)]) for _ in range(resamples))
    return point, values[int(0.025 * resamples)], values[int(0.975 * resamples) - 1]


def _table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return "\n".join(out + ["| " + " | ".join(map(str, r)) + " |" for r in rows])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--append", type=Path, help="Markdown appended to the report")
    args = parser.parse_args()

    rows, _ = dedupe(load_dataset(DEFAULT_DATASET))
    repos = sorted({r["repo"] for r in rows})

    # --- 1. Development: LORO on the training repositories ---------------------------
    dev: dict[tuple[str, float], dict[str, float]] = {}
    for name, pre in VARIANTS.items():
        texts = [pre(r) for r in rows]
        for C in C_VALUES:
            dev[(name, C)] = loro(rows, texts, C)
            print(f"LORO {name:22} C={C}: {np.mean(list(dev[(name, C)].values())):.4f}", flush=True)
    shipped_key = ("shipped preprocessing", 4.0)  # the shipped model's configuration
    candidate_key = max(
        (k for k in dev if k[0] == "template text removed"),
        key=lambda k: np.mean(list(dev[k].values())),
    )
    gains = {r: dev[candidate_key][r] - dev[shipped_key][r] for r in repos}
    loro_gain = float(np.mean(list(gains.values())))
    loro_wins = sum(g > 0 for g in gains.values())
    loro_ok = loro_gain >= 0.01 and loro_wins >= 7

    # --- 2. External test ---------------------------------------------------------------
    if not EXTERNAL.exists():
        raise SystemExit(f"{EXTERNAL} not found — run `python -m scripts.fetch_category_external`.")
    ext = [json.loads(line) for line in EXTERNAL.read_text(encoding="utf-8").splitlines() if line]
    gold = [r["category"] for r in ext]

    shipped = IssueClassifier.load(DEFAULT_MODEL)
    labels = [r["category"] for r in rows]
    train_rows, test_rows = train_test_split(rows, test_size=0.2, stratify=labels, random_state=42)
    audit_lines = audit(rows, train_rows, test_rows, shipped)
    pre = VARIANTS["template text removed"]
    candidate = fit(
        [pre(r) for r in train_rows],
        [r["category"] for r in train_rows],
        C=candidate_key[1],
        seed=42,
        vectorizer_params=VECTORIZER_PARAMS,
    )

    p_ship = shipped.predict_proba_texts([build_text(r["title"], r["body"]) for r in ext])
    p_cand = candidate.predict_proba_texts([pre(r) for r in ext])
    pred_ship = [shipped.classes[i] for i in p_ship.argmax(axis=1)]
    pred_cand = [candidate.classes[i] for i in p_cand.argmax(axis=1)]
    # The rules see title and body only: the category label is the answer.
    pred_rules = [classify_item(r["title"], r["body"], kind="issue").category for r in ext]
    m_ship, m_cand, m_rules = (evaluate(gold, p) for p in (pred_ship, pred_cand, pred_rules))
    diff = bootstrap_diff(gold, pred_cand, pred_ship)
    ext_ok = diff[0] >= 0.02 and diff[1] > 0
    adopt = loro_ok and ext_ok
    print(
        f"external macro-F1: shipped {m_ship['macro_f1']:.4f}, candidate "
        f"{m_cand['macro_f1']:.4f}, rules {m_rules['macro_f1']:.4f}; "
        f"difference {diff[0]:+.4f} [{diff[1]:+.4f}, {diff[2]:+.4f}] -> "
        f"{'ADOPT' if adopt else 'keep shipped model'}"
    )

    if not args.out:
        return

    def app_pipeline(proba: np.ndarray, classes: list[str], t: float) -> tuple[list[str], float]:
        out, used = [], 0
        for k in range(len(ext)):
            if proba[k].max() >= t:
                out.append(classes[int(proba[k].argmax())])
                used += 1
            else:
                out.append(pred_rules[k])
        return out, used / len(ext)

    threshold_rows = []
    for t in THRESHOLDS:
        decided = [k for k in range(len(ext)) if p_ship[k].max() >= t]
        acc_decided = (
            sum(pred_ship[k] == gold[k] for k in decided) / len(decided) if decided else 0.0
        )
        combined, share = app_pipeline(p_ship, shipped.classes, t)
        m = evaluate(gold, combined)
        threshold_rows.append(
            [
                f"{t:.1f}",
                f"{share:.1%}",
                f"{acc_decided:.3f}",
                f"{m['accuracy']:.3f}",
                f"{m['macro_f1']:.3f}",
            ]
        )

    counts = Counter((r["repo"], r["category"]) for r in ext)
    ext_repos = sorted({r["repo"] for r in ext})
    data_rows = [
        [repo, *(counts[(repo, c)] for c in LABELS), sum(counts[(repo, c)] for c in LABELS)]
        for repo in ext_repos
    ]
    per_repo = []
    for repo in ext_repos:
        idx = [k for k, r in enumerate(ext) if r["repo"] == repo]
        g = [gold[k] for k in idx]
        per_repo.append(
            [
                repo,
                *(
                    f"{evaluate(g, [p[k] for k in idx])['macro_f1']:.3f}"
                    for p in (pred_ship, pred_cand, pred_rules)
                ),
            ]
        )
    per_class = [
        [
            c,
            f"{m_ship['per_class'][c]['precision']:.3f}",
            f"{m_ship['per_class'][c]['recall']:.3f}",
            f"{m_ship['per_class'][c]['f1']:.3f}",
            f"{m_cand['per_class'][c]['f1']:.3f}",
            f"{m_rules['per_class'][c]['f1']:.3f}",
            m_ship["per_class"][c]["support"],
        ]
        for c in LABELS
    ]
    cm = _table(
        ["true \\ predicted", *m_ship["labels"]],
        [[t, *row] for t, row in zip(m_ship["labels"], m_ship["confusion_matrix"], strict=True)],
    )
    loro_rows = [[name, C, f"{np.mean(list(v.values())):.3f}"] for (name, C), v in dev.items()]
    gain_rows = [
        [r, f"{dev[shipped_key][r]:.3f}", f"{dev[candidate_key][r]:.3f}", f"{gains[r]:+.3f}"]
        for r in repos
    ]

    parts = [
        "# Category classifier — generalisation re-assessment (Phase 6)",
        "",
        "_Tables generated by `python -m scripts.evaluate_category_generalization`; do not edit "
        "by hand. Notes at the end come from `docs/category_generalization_notes.md`._",
        "",
        "## 0. Audit of the existing model and data",
        "",
        *audit_lines,
        "",
        "## 1. Development: leave-one-repository-out on the training data",
        "",
        "Mean macro-F1 when each of the ten training repositories is held out in turn. The "
        "shipped model uses the shipped preprocessing with C = 4.0.",
        "",
        _table(["preprocessing", "C", "LORO mean macro-F1"], loro_rows),
        "",
        f"Best candidate: **{candidate_key[0]}, C = {candidate_key[1]}** — mean gain "
        f"{loro_gain:+.3f}, better on {loro_wins} of {len(repos)} repositories "
        f"(rule: ≥ +0.010 and ≥ 7 of 10 → {'met' if loro_ok else 'not met'}).",
        "",
        _table(["held-out repository", "shipped", "candidate", "difference"], gain_rows),
        "",
        "## 2. External test: eight repositories never used before",
        "",
        _table(["repository", *LABELS, "total"], data_rows),
        "",
        _table(
            ["model", "accuracy", "macro F1"],
            [
                ["shipped model", f"{m_ship['accuracy']:.3f}", f"{m_ship['macro_f1']:.3f}"],
                [
                    f"candidate ({candidate_key[0]}, C = {candidate_key[1]})",
                    f"{m_cand['accuracy']:.3f}",
                    f"{m_cand['macro_f1']:.3f}",
                ],
                ["keyword rules", f"{m_rules['accuracy']:.3f}", f"{m_rules['macro_f1']:.3f}"],
            ],
        ),
        "",
        f"Candidate − shipped macro-F1: **{diff[0]:+.3f}** (95% paired bootstrap "
        f"[{diff[1]:+.3f}, {diff[2]:+.3f}], 2,000 resamples, seed 0). External rule "
        f"(≥ +0.020 and interval above 0): {'met' if ext_ok else 'not met'}. "
        f"**Decision: {'adopt the candidate' if adopt else 'keep the shipped model'}.**",
        "",
        "### Per class (external)",
        "",
        _table(
            [
                "class",
                "shipped precision",
                "shipped recall",
                "shipped F1",
                "candidate F1",
                "rules F1",
                "support",
            ],
            per_class,
        ),
        "",
        "Shipped model, confusion matrix (rows = maintainer label):",
        "",
        cm,
        "",
        "### Per repository (external, macro-F1)",
        "",
        _table(["repository", "shipped", "candidate", "rules"], per_repo),
        "",
        "### Confidence threshold, as the app uses it (external, shipped model)",
        "",
        "The app keeps the ML category when its probability is at least the threshold and "
        "uses the keyword rules otherwise (maintainer labels are not used here: they are the "
        "answer).",
        "",
        _table(
            [
                "threshold",
                "decided by ML",
                "ML accuracy on those",
                "app accuracy (ML + rules)",
                "app macro F1",
            ],
            threshold_rows,
        ),
        "",
    ]
    extra = args.append.read_text(encoding="utf-8") if args.append else ""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(parts) + "\n" + extra, encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
