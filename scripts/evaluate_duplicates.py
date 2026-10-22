"""Evaluate duplicate detection on real, maintainer-confirmed duplicate pairs.

Data
----
* ``data/duplicate_pairs.jsonl`` — pairs from ``scripts/fetch_duplicate_dataset.py``:
  an issue a maintainer closed as a duplicate, and the original it duplicates.
* Candidate pool — every cached issue of the same repository (``data/raw/``,
  ``data/priority_raw/``) plus the originals. TF-IDF is fitted on that pool,
  as the app fits it on a repository's imported issues.

Metrics (per query = one duplicate issue)
-----------------------------------------
* Recall@k — the original is among the k most similar issues (no threshold).
* MRR — mean of 1 / rank of the original.
* At a threshold t (up to 5 suggestions with score >= t): recall = share of
  queries whose original is suggested; precision = share of suggestions that are
  the original or another known duplicate of it. Precision is a **lower bound**:
  unlabelled real duplicates count as false positives.
* False-alarm rate — share of *non-duplicate* issues (sampled from the pool, not
  involved in any known pair) that get at least one suggestion.

Protocol: title weight and threshold are chosen on the **development**
repositories only (default ``--split dev``): the highest F1 among settings whose
false-alarm rate is at most ``MAX_FALSE_ALARM`` (a review panel that flags most
ordinary issues would be ignored). ``--split all`` then reports the held-out
test repositories once. Scores are similarities, not probabilities.

Cohorts: ``--cohort phase4`` (default) uses only the 47 Phase 4 pairs and
reproduces ``docs/DUPLICATE_EVALUATION.md``; ``--cohort all`` adds the Phase 6
pairs (``fetch_duplicate_dataset.py --expand``) and reports per-repository
results, Wilson 95% intervals, a held-out threshold sweep (for reporting only —
never used for selection) and the individual errors.

Usage:
    python -m scripts.evaluate_duplicates                    # dev: select settings
    python -m scripts.evaluate_duplicates --split all --out docs/DUPLICATE_EVALUATION.md
    python -m scripts.evaluate_duplicates --cohort all --split all \\
        --out docs/DUPLICATE_EVALUATION_PHASE6.md
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from app.duplicates import MAX_SUGGESTIONS, DuplicateIndex
from scripts.fetch_duplicate_dataset import PAIRS_FILE, cached_issues

WEIGHTS = (0.4, 0.5, 0.6, 0.7, 0.8, 1.0)
THRESHOLDS = tuple(round(0.05 + 0.05 * i, 2) for i in range(16))  # 0.05 … 0.80
NEGATIVES_PER_REPO = 30
MAX_FALSE_ALARM = 0.10  # at most ~1 in 10 non-duplicate issues may get a suggestion
TOP = 10


def _in_cohort(pair: dict, cohort: str) -> bool:
    return cohort == "all" or pair.get("cohort", "phase4") == cohort


def load(
    split: str, cohort: str = "phase4"
) -> tuple[list[dict], dict[str, dict[int, dict]], dict[str, list[int]]]:
    if not PAIRS_FILE.exists():
        raise SystemExit(
            f"{PAIRS_FILE} not found — run `python -m scripts.fetch_duplicate_dataset`."
        )
    pairs = [
        json.loads(line) for line in PAIRS_FILE.read_text(encoding="utf-8").splitlines() if line
    ]
    pairs = [p for p in pairs if _in_cohort(p, cohort)]
    if split == "dev":
        pairs = [p for p in pairs if p["split"] == "dev"]
    pools: dict[str, dict[int, dict]] = defaultdict(dict)
    for (repo, number), raw in cached_issues().items():
        pools[repo][number] = {
            "number": number,
            "title": raw.get("title") or "",
            "body": raw.get("body") or "",
        }
    for p in pairs:
        for side in ("duplicate", "canonical"):
            issue = p[side]
            pools[p["repo"]].setdefault(
                issue["number"],
                {"number": issue["number"], "title": issue["title"], "body": issue["body"]},
            )
    involved = defaultdict(set)
    all_pairs = [
        p
        for p in (
            json.loads(line) for line in PAIRS_FILE.read_text(encoding="utf-8").splitlines() if line
        )
        if _in_cohort(p, cohort)
    ]
    for p in all_pairs:  # exclude every known pair member from the negatives
        involved[p["repo"]] |= {p["duplicate"]["number"], p["canonical"]["number"]}
    negatives: dict[str, list[int]] = {}
    for repo in {p["repo"] for p in pairs}:
        candidates = sorted(n for n in pools[repo] if n not in involved[repo])
        step = max(1, len(candidates) // NEGATIVES_PER_REPO)
        negatives[repo] = candidates[::step][:NEGATIVES_PER_REPO]
    return pairs, {r: pools[r] for r in {p["repo"] for p in pairs}}, negatives


def rank_queries(pairs, pools, negatives, weight: float) -> tuple[list[dict], list[list[float]]]:
    """Ranked candidates for every duplicate query and every negative query."""
    truth: dict[tuple[str, int], set[int]] = defaultdict(set)
    by_canonical = defaultdict(set)
    for p in pairs:
        by_canonical[(p["repo"], p["canonical"]["number"])].add(p["duplicate"]["number"])
    for p in pairs:
        repo, d, c = p["repo"], p["duplicate"]["number"], p["canonical"]["number"]
        truth[(repo, d)] = {c} | (by_canonical[(repo, c)] - {d})

    queries, negative_scores = [], []
    for repo, pool in sorted(pools.items()):
        numbers = sorted(pool)
        position = {n: i for i, n in enumerate(numbers)}
        index = DuplicateIndex([pool[n] for n in numbers], title_weight=weight)
        for p in (p for p in pairs if p["repo"] == repo):
            d = p["duplicate"]["number"]
            ranked = [
                (numbers[m.b if m.a == position[d] else m.a], m.score)
                for m in index.rank(position[d], top=TOP)
            ]
            queries.append(
                {
                    "repo": repo,
                    "split": p["split"],
                    "duplicate": d,
                    "canonical": p["canonical"]["number"],
                    "truth": truth[(repo, d)],
                    "ranked": ranked,
                }
            )
        for n in negatives.get(repo, []):
            negative_scores.append([m.score for m in index.rank(position[n], top=MAX_SUGGESTIONS)])
    return queries, negative_scores


def metrics(queries: list[dict], negative_scores: list[list[float]], threshold: float) -> dict:
    n = len(queries)
    rr, hit1, hit5, suggested, correct, found = 0.0, 0, 0, 0, 0, 0
    for q in queries:
        ranks = [i for i, (num, _) in enumerate(q["ranked"]) if num in q["truth"]]
        if ranks:
            rr += 1 / (ranks[0] + 1)
            hit1 += ranks[0] == 0
            hit5 += ranks[0] < 5
        shown = [num for num, s in q["ranked"][:MAX_SUGGESTIONS] if s >= threshold]
        suggested += len(shown)
        correct += sum(num in q["truth"] for num in shown)
        found += any(num in q["truth"] for num in shown)
    precision = correct / suggested if suggested else 0.0
    recall = found / n if n else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    alarms = sum(any(s >= threshold for s in scores) for scores in negative_scores)
    return {
        "queries": n,
        "recall_at_1": hit1 / n if n else 0.0,
        "recall_at_5": hit5 / n if n else 0.0,
        "mrr": rr / n if n else 0.0,
        "threshold": threshold,
        "suggestions": suggested,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "negatives": len(negative_scores),
        "false_alarm_rate": alarms / len(negative_scores) if negative_scores else 0.0,
    }


def select(
    results: dict[float, tuple[list, list]], max_false_alarm: float | None = MAX_FALSE_ALARM
) -> tuple[float, float, dict]:
    """Best (weight, threshold) by dev F1 among settings within the false-alarm cap.

    Ties -> higher threshold, then higher weight. ``max_false_alarm=None`` removes
    the cap (reported for comparison).
    """
    best = None
    for weight, (queries, negs) in results.items():
        dev_q = [q for q in queries if q["split"] == "dev"]
        dev_n = negs["dev"]
        for t in THRESHOLDS:
            m = metrics(dev_q, dev_n, t)
            if max_false_alarm is not None and m["false_alarm_rate"] > max_false_alarm:
                continue
            candidate = (m["f1"], t, weight)
            if best is None or candidate > best[0]:
                best = (candidate, weight, t, m)
    if best is None:
        raise SystemExit("no setting satisfies the false-alarm cap")
    return best[1], best[2], best[3]


def _table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return "\n".join(out + ["| " + " | ".join(map(str, r)) + " |" for r in rows])


def _row(name, m):
    return [
        name,
        m["queries"],
        f"{m['recall_at_1']:.3f}",
        f"{m['recall_at_5']:.3f}",
        f"{m['mrr']:.3f}",
        f"{m['threshold']:.2f}",
        f"{m['precision']:.3f}",
        f"{m['recall']:.3f}",
        f"{m['f1']:.3f}",
        f"{m['false_alarm_rate']:.3f}",
    ]


HEADER = [
    "setting",
    "duplicate queries",
    "recall@1",
    "recall@5",
    "MRR",
    "threshold",
    "precision (lower bound)",
    "recall",
    "F1",
    "false-alarm rate",
]


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion (sensible for small n)."""
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def counts(queries: list[dict], threshold: float) -> tuple[int, int, int, int]:
    """(queries with the original suggested, queries, correct suggestions, suggestions)."""
    found = suggested = correct = 0
    for q in queries:
        shown = [num for num, s in q["ranked"][:MAX_SUGGESTIONS] if s >= threshold]
        suggested += len(shown)
        correct += sum(num in q["truth"] for num in shown)
        found += any(num in q["truth"] for num in shown)
    return found, len(queries), correct, suggested


def phase6_sections(
    results: dict, weight: float, threshold: float, title_only_t: float
) -> list[str]:
    """Extra tables for the expanded dataset: intervals, per repository, sweep, errors."""
    queries, negs = results[weight]
    parts = ["## Uncertainty (Wilson 95% intervals)", ""]
    rows = []
    for split in ("dev", "test"):
        q = [x for x in queries if x["split"] == split]
        if not q:
            continue
        found, n, correct, suggested = counts(q, threshold)
        r_lo, r_hi = wilson(found, n)
        p_lo, p_hi = wilson(correct, suggested)
        alarms = sum(any(s >= threshold for s in sc) for sc in negs[split])
        a_lo, a_hi = wilson(alarms, len(negs[split]))
        rows.append(
            [
                split,
                f"{found}/{n} = {found / n:.3f} [{r_lo:.2f}, {r_hi:.2f}]",
                f"{correct}/{suggested} = {correct / max(1, suggested):.3f} "
                f"[{p_lo:.2f}, {p_hi:.2f}]",
                f"{alarms}/{len(negs[split])} = {alarms / max(1, len(negs[split])):.3f} "
                f"[{a_lo:.2f}, {a_hi:.2f}]",
            ]
        )
    parts += [
        _table(["split", "recall", "precision (lower bound)", "false-alarm rate"], rows),
        "",
        "## Results by repository (chosen setting)",
        "",
        "Small per-repository samples: read these as descriptions, not estimates.",
        "",
    ]
    rows = []
    for repo in sorted({q["repo"] for q in queries}):
        q = [x for x in queries if x["repo"] == repo]
        found, n, correct, suggested = counts(q, threshold)
        m = metrics(q, [], threshold)
        rows.append(
            [
                repo,
                q[0]["split"],
                n,
                f"{found}/{n}",
                f"{correct}/{suggested}",
                f"{m['recall_at_5']:.2f}",
            ]
        )
    parts += [
        _table(
            [
                "repository",
                "split",
                "pairs",
                "original suggested",
                "correct / all suggestions",
                "recall@5",
            ],
            rows,
        ),
        "",
        "## Held-out threshold sweep (reporting only)",
        "",
        f"Title weight {weight}. **Not used to choose anything**: the threshold was fixed "
        "on the development split above. Shown so readers can see the trade-off.",
        "",
    ]
    test_q = [x for x in queries if x["split"] == "test"]
    sweep = []
    for t in THRESHOLDS:
        m = metrics(test_q, negs["test"], t)
        sweep.append(
            [
                f"{t:.2f}",
                f"{m['precision']:.3f}",
                f"{m['recall']:.3f}",
                f"{m['f1']:.3f}",
                f"{m['false_alarm_rate']:.3f}",
            ]
        )
    parts += [
        _table(["threshold", "precision (lower bound)", "recall", "F1", "false-alarm rate"], sweep),
        "",
        "## Held-out errors (chosen setting)",
        "",
        "Issue numbers only (text stays local). *Suggested but unlabelled* pairs may "
        "still be real duplicates nobody marked.",
        "",
    ]
    fp_rows, fn_rows = [], []
    for q in test_q:
        shown = [(num, s) for num, s in q["ranked"][:MAX_SUGGESTIONS] if s >= threshold]
        for num, s in shown:
            if num not in q["truth"]:
                fp_rows.append([q["repo"], f"#{q['duplicate']}", f"#{num}", f"{s:.2f}"])
        if not any(num in q["truth"] for num, _ in shown):
            rank = next(
                (i + 1 for i, (num, _) in enumerate(q["ranked"]) if num in q["truth"]), None
            )
            score = next((s for num, s in q["ranked"] if num in q["truth"]), None)
            fn_rows.append(
                [
                    q["repo"],
                    f"#{q['duplicate']}",
                    f"#{q['canonical']}",
                    str(rank) if rank else f"> {TOP}",
                    f"{score:.2f}" if score is not None else "-",
                ]
            )
    parts += [
        f"**Suggested, not a labelled duplicate** ({len(fp_rows)}):",
        "",
        _table(["repository", "query", "suggested", "score"], fp_rows) if fp_rows else "_none_",
        "",
        f"**Labelled original not suggested** ({len(fn_rows)}):",
        "",
        _table(
            ["repository", "query", "labelled original", "rank of original", "its score"], fn_rows
        )
        if fn_rows
        else "_none_",
        "",
    ]
    return parts


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate duplicate detection.")
    parser.add_argument("--split", choices=["dev", "all"], default="dev")
    parser.add_argument(
        "--cohort",
        choices=["phase4", "all"],
        default="phase4",
        help="phase4: the original 47 pairs; all: with the Phase 6 expansion",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--append", type=Path, help="Markdown appended to the report")
    args = parser.parse_args()

    pairs, pools, negatives = load(args.split, args.cohort)
    results = {}
    for weight in WEIGHTS:
        queries, _ = rank_queries(pairs, pools, negatives, weight)
        negs = {}
        for split in ("dev", "test"):
            repos = {p["repo"] for p in pairs if p["split"] == split}
            split_pools = {r: pools[r] for r in repos}
            negs[split] = rank_queries(
                [p for p in pairs if p["split"] == split],
                split_pools,
                {r: negatives[r] for r in repos},
                weight,
            )[1]
        results[weight] = (queries, negs)

    weight, threshold, dev_best = select(results)
    print(
        f"selected on dev: title weight {weight}, threshold {threshold:.2f} · "
        f"dev F1 {dev_best['f1']:.3f} (P {dev_best['precision']:.3f}, R {dev_best['recall']:.3f}, "
        f"false alarms {dev_best['false_alarm_rate']:.3f})"
    )
    uncapped_w, uncapped_t, uncapped = select(results, max_false_alarm=None)
    print(
        f"(without the false-alarm cap: title weight {uncapped_w}, threshold {uncapped_t:.2f}, "
        f"dev F1 {uncapped['f1']:.3f}, false alarms {uncapped['false_alarm_rate']:.3f})"
    )
    _, title_only_t, _ = select({1.0: results[1.0]})

    rows_by_split = {}
    for split in ("dev", "test"):
        q = [x for x in results[weight][0] if x["split"] == split]
        if not q:
            continue
        m = metrics(q, results[weight][1][split], threshold)
        q1 = [x for x in results[1.0][0] if x["split"] == split]
        m1 = metrics(q1, results[1.0][1][split], title_only_t)
        rows_by_split[split] = (m, m1)
        print(
            f"{split}: queries {m['queries']} · recall@1 {m['recall_at_1']:.3f} · recall@5 "
            f"{m['recall_at_5']:.3f} · MRR {m['mrr']:.3f} · P {m['precision']:.3f} · "
            f"R {m['recall']:.3f} · F1 {m['f1']:.3f} · false alarms {m['false_alarm_rate']:.3f}"
        )

    if not args.out:
        return
    dev_q = [q for q in results[weight][0] if q["split"] == "dev"]
    sweep = []
    for t in THRESHOLDS:
        m = metrics(dev_q, results[weight][1]["dev"], t)
        sweep.append(
            [
                f"{t:.2f}",
                f"{m['precision']:.3f}",
                f"{m['recall']:.3f}",
                f"{m['f1']:.3f}",
                f"{m['false_alarm_rate']:.3f}",
            ]
        )
    weight_rows = []
    for w in WEIGHTS:
        w_dev = [q for q in results[w][0] if q["split"] == "dev"]
        best_f1 = max(metrics(w_dev, results[w][1]["dev"], t)["f1"] for t in THRESHOLDS)
        mrr = metrics(w_dev, results[w][1]["dev"], 0.0)["mrr"]
        weight_rows.append([w, f"{best_f1:.3f}", f"{mrr:.3f}"])
    counts = defaultdict(lambda: [0, 0])
    for p in pairs:
        counts[(p["split"], p["repo"])][0] += 1
    for repo, pool in pools.items():
        split = next(p["split"] for p in pairs if p["repo"] == repo)
        counts[(split, repo)][1] = len(pool)
    dataset = [
        [repo, split, n, size, len(negatives.get(repo, []))]
        for (split, repo), (n, size) in sorted(counts.items())
    ]
    test_q = [q for q in results[weight][0] if q["split"] == "test"]
    misses = [
        q
        for q in test_q
        if not any(n in q["truth"] for n, s in q["ranked"][:MAX_SUGGESTIONS] if s >= threshold)
    ]
    notes = (
        args.append.as_posix()
        if args.append and not args.append.is_absolute()
        else "docs/duplicate_evaluation_notes.md"
    )
    parts = [
        "# Duplicate detection — evaluation on real duplicate pairs",
        "",
        f"_Tables generated by `python -m scripts.evaluate_duplicates "
        f"{'--cohort all ' if args.cohort == 'all' else ''}--split all`; do not edit "
        f"by hand. Analysis notes at the end come from `{notes}`._",
        "",
        "## Dataset",
        "",
        _table(
            ["repository", "split", "duplicate pairs", "candidate pool", "non-duplicate queries"],
            dataset,
        ),
        "",
        "## Selection (development split only)",
        "",
        f"Rule: highest development F1 among settings with a false-alarm rate of at most "
        f"{MAX_FALSE_ALARM:.2f} (ties prefer the higher threshold). Chosen: "
        f"**title weight {weight}**, **threshold {threshold:.2f}** (dev F1 "
        f"{dev_best['f1']:.3f}, false alarms {dev_best['false_alarm_rate']:.3f}). Without the "
        f"cap, F1 alone would pick title weight {uncapped_w}, threshold {uncapped_t:.2f} "
        f"(dev F1 {uncapped['f1']:.3f}) — but with a false-alarm rate of "
        f"{uncapped['false_alarm_rate']:.3f}.",
        "",
        _table(["title weight", "best dev F1", "dev MRR"], weight_rows),
        "",
        f"Threshold sweep at title weight {weight} (development split):",
        "",
        _table(["threshold", "precision (lower bound)", "recall", "F1", "false-alarm rate"], sweep),
        "",
    ]
    for split, (m, m1) in rows_by_split.items():
        parts += [
            f"## Results — {split} split",
            "",
            _table(
                HEADER,
                [
                    _row(f"title {weight} + description {1 - weight:.1f}", m),
                    _row("title only (threshold chosen on dev)", m1),
                ],
            ),
            "",
        ]
    parts += [
        f"Held-out test queries whose original was **not** suggested: {len(misses)} of "
        f"{len(test_q)}.",
        "",
    ]
    if args.cohort == "all":
        parts += phase6_sections(results, weight, threshold, title_only_t)
    extra = args.append.read_text(encoding="utf-8") if args.append else ""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(parts) + "\n" + extra, encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
