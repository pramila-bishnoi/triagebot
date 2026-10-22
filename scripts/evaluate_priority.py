"""Evaluate priority suggestions against maintainer priority labels.

Main dataset: ``data/priority_issues.jsonl`` from ``scripts/fetch_priority_dataset.py``
— 6 repositories, split **by repository** into development and held-out test
repos (the split is fixed in that script). The maintainer's priority label is
the reference answer and was removed from each issue's labels when the
dataset was built, so the scorer cannot see it.

Protocol: develop and analyse errors on ``--split dev`` only (the default);
score ``--split all`` once, at the end, to report held-out test results.

``--pipeline app`` (default) scores each issue the way the dashboard does,
through ``app.ml.service.classify``: the category comes from a maintainer's
category label, the ML model or the rules, and priority is then scored for that
category. ``--pipeline rules`` uses ``app.triage.classify_item`` only (keyword
rules, no ML). Both are deterministic. Note that the category model was trained
on issues from two of the development repositories (microsoft/terminal,
tauri-apps/tauri), so category quality there is optimistic; the test repositories
are not in its training data. Baselines are computed in this script.

Usage (from the repository root):
    python -m scripts.evaluate_priority                       # dev split summary
    python -m scripts.evaluate_priority --split all --save-predictions p.json
    python -m scripts.evaluate_priority --split all --out docs/PRIORITY_EVALUATION.md \\
        --compare "Phase 2 scorer=phase2.json"

To score with another version of the code (e.g. the Phase 2 scorer at commit
``6dec35d``), export that version and put it first on ``PYTHONPATH``:
    mkdir /tmp/phase2 && git archive 6dec35d app | tar -x -C /tmp/phase2
    PYTHONPATH=/tmp/phase2 python scripts/evaluate_priority.py --split all \\
        --save-predictions phase2.json

Phase 6 (``--cohort all``) adds 15 repositories as splits ``dev2`` / ``test2``
(see ``scripts/fetch_priority_dataset.py``). ``--cohort v1`` (default) keeps
only the original six repositories and reproduces ``docs/PRIORITY_EVALUATION.md``.
With ``--cohort all`` the report also has "all development" / "all held-out"
groups, high→low and low→high errors separately, and per-repository results.

    python -m scripts.evaluate_priority --cohort all --split all --baselines \
        --bootstrap-split "all held-out" --out docs/PRIORITY_EVALUATION_PHASE6.md

``--dataset category-sample`` reproduces the earlier 49-issue check on the
category dataset cache (``data/raw/``).
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path

from app.ml import service as ml_service
from app.triage import classify_item

ROOT = Path(__file__).resolve().parent.parent
PRIORITY_FILE = ROOT / "data" / "priority_issues.jsonl"
RAW_DIR = ROOT / "data" / "raw"
LEVELS = ["high", "medium", "low"]
RANK = {"low": 0, "medium": 1, "high": 2}


# --- Data ------------------------------------------------------------------------


def load_priority_dataset(path: Path = PRIORITY_FILE) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"{path} not found — run `python -m scripts.fetch_priority_dataset`.")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [{**r, "gold": r["level"]} for r in rows]


def legacy_gold_level(label: str) -> str | None:
    """Label mapping used by the earlier 49-issue check (category dataset cache)."""
    name = re.sub(r"[\s:/_\-]+", " ", label.lower()).strip()
    match = re.match(r"^(?:priority ?|p)([0-4])\b", name)
    if match:
        return {"0": "high", "1": "high", "2": "medium", "3": "low", "4": "low"}[match[1]]
    match = re.match(r"^(?:priority (high|medium|low)|(high|medium|low) priority)$", name)
    if match:
        return match[1] or match[2]
    return None


def load_category_sample(raw_dir: Path = RAW_DIR) -> list[dict]:
    """The earlier check: category-dataset issues that happen to carry a priority label."""
    if not raw_dir.exists():
        raise SystemExit(f"{raw_dir} not found — run `python -m scripts.fetch_dataset` first.")
    issues: dict[tuple[str, int], dict] = {}
    for path in sorted(raw_dir.glob("*.json")):
        for raw in json.loads(path.read_text(encoding="utf-8")):
            issues[(raw.get("repository_url", "").split("/repos/")[-1], raw["number"])] = raw
    examples = []
    for (repo, number), raw in sorted(issues.items()):
        labels = [lbl["name"] for lbl in raw.get("labels", [])]
        levels = {legacy_gold_level(lbl) for lbl in labels} - {None}
        if len(levels) != 1:
            continue
        examples.append(
            {
                "repo": repo,
                "number": number,
                "split": "sample",
                "gold": levels.pop(),
                "title": raw.get("title") or "",
                "body": raw.get("body") or "",
                "labels": [lbl for lbl in labels if legacy_gold_level(lbl) is None],
                "reactions": (raw.get("reactions") or {}).get("total_count", 0),
                "comments": raw.get("comments", 0),
            }
        )
    return examples


def key(ex: dict) -> str:
    return f"{ex['repo']}#{ex['number']}"


# --- Scoring and metrics -------------------------------------------------------------


def predict(examples: list[dict], pipeline: str = "app") -> dict[str, str]:
    score = ml_service.classify if pipeline == "app" else classify_item
    return {
        key(ex): score(
            ex["title"],
            ex["body"],
            kind="issue",
            labels=ex["labels"],
            reactions=ex["reactions"],
            comments=ex["comments"],
        ).priority
        for ex in examples
    }


def metrics(gold: list[str], pred: list[str]) -> dict:
    n = len(gold)
    matrix = [
        [sum(1 for g, p in zip(gold, pred, strict=True) if g == t and p == q) for q in LEVELS]
        for t in LEVELS
    ]
    per_level = {}
    for i, level in enumerate(LEVELS):
        tp, predicted, actual = matrix[i][i], sum(r[i] for r in matrix), sum(matrix[i])
        precision = tp / predicted if predicted else 0.0
        recall = tp / actual if actual else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_level[level] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": actual,
            "predicted": predicted,
        }
    return {
        "n": n,
        "accuracy": sum(g == p for g, p in zip(gold, pred, strict=True)) / n if n else 0.0,
        "macro_f1": sum(m["f1"] for m in per_level.values()) / len(LEVELS),
        # ordinal error: 0 = exact, 1 = one level off, 2 = high<->low
        "mean_abs_level_error": (
            sum(abs(RANK[g] - RANK[p]) for g, p in zip(gold, pred, strict=True)) / n if n else 0.0
        ),
        "two_level_errors": matrix[0][2] + matrix[2][0],
        "per_level": per_level,
        "confusion_matrix": matrix,
        "predicted_distribution": {lv: sum(r[i] for r in matrix) for i, lv in enumerate(LEVELS)},
    }


COMBINED = {"all development": ("dev", "dev2"), "all held-out": ("test", "test2")}


def evaluate(examples: list[dict], preds: dict[str, str]) -> dict:
    groups: dict[str, list[dict]] = {}
    expanded = any(ex["split"] in ("dev2", "test2") for ex in examples)
    for ex in examples:
        groups.setdefault(ex["split"], []).append(ex)
        groups.setdefault(f"repo:{ex['repo']}", []).append(ex)
        if expanded:
            for name, members in COMBINED.items():
                if ex["split"] in members:
                    groups.setdefault(name, []).append(ex)
    out = {
        name: metrics([e["gold"] for e in exs], [preds[key(e)] for e in exs])
        for name, exs in groups.items()
    }
    out["all"] = metrics([e["gold"] for e in examples], [preds[key(e)] for e in examples])
    return out


def baselines(examples: list[dict]) -> dict[str, dict[str, str]]:
    dev = [e["gold"] for e in examples if e["split"] in ("dev", "dev2", "sample")]
    majority = Counter(dev).most_common(1)[0][0] if dev else "medium"
    runs = {f"always {lv}": dict.fromkeys(map(key, examples), lv) for lv in LEVELS}
    runs[f"development majority ('{majority}')"] = dict.fromkeys(map(key, examples), majority)
    return runs


def signal_table(examples: list[dict]) -> str:
    """How often each scorer signal co-occurs with each maintainer level (rules category)."""
    from app.matching import clean_body, find_all_keywords, normalize
    from app.priority import TEXT_SIGNALS, is_severity_topic_label

    flags: dict[str, list[dict]] = {}
    for ex in examples:
        title, body = normalize(ex["title"]), normalize(clean_body(ex["body"]))
        for signal, _points, words in TEXT_SIGNALS:
            in_title = find_all_keywords(title, words)[0]
            in_body = find_all_keywords(body, words)[0]
            if in_title:
                flags.setdefault(f"{signal} wording in title", []).append(ex)
            elif in_body:
                flags.setdefault(f"{signal} wording in body only", []).append(ex)
        if any(is_severity_topic_label(lbl) for lbl in ex["labels"]):
            flags.setdefault("severity topic label", []).append(ex)
        if ex["reactions"] >= 10:
            flags.setdefault(">= 10 reactions", []).append(ex)
        if ex["comments"] >= 10:
            flags.setdefault(">= 10 comments", []).append(ex)
        category = classify_item(
            ex["title"], ex["body"], kind="issue", labels=ex["labels"]
        ).category
        flags.setdefault(f"rules category: {category}", []).append(ex)
    rows = [
        [
            "(all issues)",
            str(len(examples)),
            *(f"{sum(e['gold'] == lv for e in examples) / len(examples):.0%}" for lv in LEVELS),
        ]
    ]
    for name in sorted(flags):
        group = flags[name]
        rows.append(
            [
                name,
                str(len(group)),
                *(f"{sum(e['gold'] == lv for e in group) / len(group):.0%}" for lv in LEVELS),
            ]
        )
    return _table(["signal", "issues", "maintainer high", "medium", "low"], rows)


def paired_bootstrap(
    examples: list[dict], a: dict[str, str], b: dict[str, str], resamples: int = 2000, seed: int = 0
) -> dict[str, tuple[float, float, float]]:
    """95% CI of (a − b) for accuracy and macro-F1, resampling issues with replacement."""
    rng = random.Random(seed)
    gold = [e["gold"] for e in examples]
    pa, pb = [a[key(e)] for e in examples], [b[key(e)] for e in examples]
    n = len(gold)

    def diff(idx: list[int]) -> tuple[float, float]:
        ma = metrics([gold[i] for i in idx], [pa[i] for i in idx])
        mb = metrics([gold[i] for i in idx], [pb[i] for i in idx])
        return ma["accuracy"] - mb["accuracy"], ma["macro_f1"] - mb["macro_f1"]

    point = diff(list(range(n)))
    samples = [diff([rng.randrange(n) for _ in range(n)]) for _ in range(resamples)]
    out = {}
    for j, name in enumerate(("accuracy", "macro_f1")):
        values = sorted(s[j] for s in samples)
        out[name] = (point[j], values[int(0.025 * resamples)], values[int(0.975 * resamples) - 1])
    return out


# --- Report ------------------------------------------------------------------------


def _table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return "\n".join(out + ["| " + " | ".join(r) + " |" for r in rows])


def _summary(runs: list[tuple[str, dict]], group: str, split_errors: bool = False) -> str:
    rows = []
    for name, res in runs:
        m = res.get(group)
        if not m:
            continue
        cm = m["confusion_matrix"]
        errors = f"{cm[0][2]} / {cm[2][0]}" if split_errors else str(m["two_level_errors"])
        rows.append(
            [
                name,
                f"{m['accuracy']:.3f}",
                f"{m['macro_f1']:.3f}",
                f"{m['per_level']['high']['recall']:.3f}",
                f"{m['per_level']['high']['precision']:.3f}",
                f"{m['per_level']['low']['recall']:.3f}",
                f"{m['mean_abs_level_error']:.3f}",
                errors,
                " / ".join(str(m["predicted_distribution"][lv]) for lv in LEVELS),
            ]
        )
    return _table(
        [
            "scorer",
            "accuracy",
            "macro F1",
            "high recall",
            "high precision",
            "low recall",
            "mean level error",
            "high→low / low→high errors" if split_errors else "high↔low errors",
            "suggested high / medium / low",
        ],
        rows,
    )


def _per_class(m: dict) -> str:
    return _table(
        ["level", "precision", "recall", "F1", "support"],
        [
            [lv, f"{x['precision']:.3f}", f"{x['recall']:.3f}", f"{x['f1']:.3f}", str(x["support"])]
            for lv, x in m["per_level"].items()
        ],
    )


def _matrix(m: dict) -> str:
    return _table(
        ["maintainer \\ suggested", *LEVELS],
        [[lv, *map(str, row)] for lv, row in zip(LEVELS, m["confusion_matrix"], strict=True)],
    )


def render(
    examples: list[dict],
    main: tuple[str, dict],
    others: list[tuple[str, dict]],
    extra_sections: str = "",
    notes: str = "docs/priority_evaluation_notes.md",
) -> str:
    name, res = main
    runs = [main, *others]
    expanded = "all held-out" in res
    splits = [
        s
        for s in ("dev", "dev2", "all development", "test", "test2", "all held-out", "sample")
        if s in res
    ]
    dist = Counter((e["repo"], e["split"], e["gold"]) for e in examples)
    repos = sorted({(e["split"], e["repo"]) for e in examples})
    dist_rows = [
        [
            repo,
            split,
            *(str(dist[(repo, split, lv)]) for lv in LEVELS),
            str(sum(dist[(repo, split, lv)] for lv in LEVELS)),
        ]
        for split, repo in repos
    ]
    parts = [
        "# Priority scoring — evaluation against maintainer priority labels",
        "",
        "_Tables generated by `python -m scripts.evaluate_priority`; do not edit by hand. "
        f"Analysis notes at the end come from `{notes}`._",
        "",
        "## Dataset",
        "",
        _table(["repository", "split", "high", "medium", "low", "total"], dist_rows),
        "",
    ]
    for split in splits:
        parts += [
            f"## Results — {split} split",
            "",
            _summary(runs, split, expanded),
            "",
            f"**{name}, {split} split — per class**",
            "",
            _per_class(res[split]),
            "",
            f"**{name}, {split} split — confusion matrix**",
            "",
            _matrix(res[split]),
            "",
        ]
    per_repo = [
        [r.removeprefix("repo:"), *(f"{res2[r]['accuracy']:.3f}" for _, res2 in runs if r in res2)]
        for r in sorted(k for k in res if k.startswith("repo:"))
    ]
    parts += [
        "## Accuracy per repository",
        "",
        _table(["repository", *(n for n, _ in runs)], per_repo),
        "",
    ]
    if expanded:
        split_of = {e["repo"]: e["split"] for e in examples}
        rows = [
            [
                r.removeprefix("repo:"),
                split_of[r.removeprefix("repo:")],
                f"{res[r]['macro_f1']:.3f}",
                f"{res[r]['confusion_matrix'][0][2]} / {res[r]['confusion_matrix'][2][0]}",
            ]
            for r in sorted(k for k in res if k.startswith("repo:"))
        ]
        parts += [
            f"## {name}: macro-F1 and extreme errors per repository",
            "",
            _table(["repository", "split", "macro F1", "high→low / low→high"], rows),
            "",
        ]
    return "\n".join(parts) + extra_sections


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate priority suggestions.")
    parser.add_argument("--dataset", choices=["priority", "category-sample"], default="priority")
    parser.add_argument(
        "--cohort",
        choices=["v1", "all"],
        default="v1",
        help="v1: the original six repositories; all: with the Phase 6 expansion",
    )
    parser.add_argument(
        "--split",
        choices=["dev", "all"],
        default="dev",
        help="'dev' while developing; 'all' once, to report held-out test",
    )
    parser.add_argument(
        "--pipeline",
        choices=["app", "rules"],
        default="app",
        help="'app' = category from label/ML/rules as in the dashboard",
    )
    parser.add_argument("--name", default="current scorer")
    parser.add_argument("--save-predictions", type=Path, help="write predictions as JSON")
    parser.add_argument(
        "--compare",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="include another run's saved predictions (repeatable)",
    )
    parser.add_argument("--baselines", action="store_true", help="include trivial baselines")
    parser.add_argument(
        "--bootstrap-split",
        default="test",
        help="split for paired bootstrap CIs of current vs each --compare run",
    )
    parser.add_argument("--out", type=Path, help="write a Markdown report")
    parser.add_argument("--append", type=Path, help="Markdown file appended to the report")
    args = parser.parse_args()

    if args.dataset == "priority":
        examples = load_priority_dataset()
        if args.cohort == "v1":
            examples = [e for e in examples if e["split"] in ("dev", "test")]
        if args.split == "dev":
            examples = [e for e in examples if e["split"] in ("dev", "dev2")]
    else:
        examples = load_category_sample()

    if args.pipeline == "app":
        status = ml_service.model_status()
        print("ML model:", "loaded" if status["available"] else f"unavailable ({status['reason']})")
    preds = predict(examples, args.pipeline)
    result = evaluate(examples, preds)
    for group in ("dev", "dev2", "all development", "test", "test2", "all held-out", "sample"):
        if group in result:
            m = result[group]
            print(
                f"{group}: {m['n']} issues · accuracy {m['accuracy']:.3f} · macro-F1 "
                f"{m['macro_f1']:.3f} · mean level error {m['mean_abs_level_error']:.3f} · "
                f"high<->low {m['two_level_errors']}"
            )
    if args.save_predictions:
        args.save_predictions.write_text(json.dumps(preds, indent=1) + "\n", encoding="utf-8")
    if args.out:
        others, ci_rows = [], []
        boot_splits = COMBINED.get(args.bootstrap_split, (args.bootstrap_split,))
        boot = [e for e in examples if e["split"] in boot_splits]
        for spec in args.compare:
            label, _, path = spec.partition("=")
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
            others.append((label, evaluate(examples, {key(e): saved[key(e)] for e in examples})))
            if boot:
                ci = paired_bootstrap(boot, preds, saved)
                ci_rows.append(
                    [
                        f"{args.name} − {label}",
                        *(
                            f"{ci[m][0]:+.3f} [{ci[m][1]:+.3f}, {ci[m][2]:+.3f}]"
                            for m in ("accuracy", "macro_f1")
                        ),
                    ]
                )
        if args.baselines:
            others += [(n, evaluate(examples, p)) for n, p in baselines(examples).items()]
        extra = ""
        dev = [e for e in examples if e["split"] in ("dev", "dev2")]
        if dev:
            extra += "\n".join(
                [
                    "## Signal analysis (development split only)",
                    "",
                    "Share of maintainer levels among development issues that show each signal "
                    "(category from the keyword rules). "
                    + (
                        "Used for the Phase 6 analysis (see the notes)."
                        if args.cohort == "all"
                        else "Used to decide the Phase 3.1 changes."
                    ),
                    "",
                    signal_table(dev),
                    "",
                    "",
                ]
            )
        if ci_rows:
            extra += "\n".join(
                [
                    f"## Are the differences real? (paired bootstrap, {args.bootstrap_split} split)",
                    "",
                    "Difference with 95% confidence interval from 2,000 paired resamples of "
                    "issues (seed 0). An interval that includes 0 means the difference could "
                    "be noise.",
                    "",
                    _table(["comparison", "accuracy difference", "macro-F1 difference"], ci_rows),
                    "",
                    "",
                ]
            )
        if args.append:
            extra += args.append.read_text(encoding="utf-8")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        notes = (
            str(args.append.relative_to(ROOT))
            if args.append and args.append.is_absolute() and args.append.is_relative_to(ROOT)
            else str(args.append)
            if args.append
            else "docs/priority_evaluation_notes.md"
        )
        args.out.write_text(
            render(examples, (args.name, result), others, extra, notes), encoding="utf-8"
        )
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
