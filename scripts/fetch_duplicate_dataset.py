"""Build a ground-truth set of real duplicate issue pairs for evaluation.

Source
------
Issues already cached by ``scripts/fetch_dataset.py`` (``data/raw/``) and
``scripts/fetch_priority_dataset.py`` (``data/priority_raw/``) that maintainers
closed as duplicates (``state_reason == "duplicate"``) or labelled as
duplicates. For each one, the issue timeline is fetched and the original
("canonical") issue is read from a maintainer comment such as
"Duplicate of #48441" or "/dup #123". The canonical issue is then fetched if it
is not cached. Pull requests, other repositories and self-references are
skipped. The other cached issues of the same repository serve as
non-duplicate candidates during evaluation.

Split — by repository, decided from pair *counts* only, before any evaluation
metric was computed: repositories are sorted by number of pairs (descending,
ties by name) and assigned alternately to development and test, starting with
development (``assign_splits``). A first plan reserved tauri-apps/tauri and
pydantic/pydantic for testing, but those projects close duplicates without a
"Duplicate of #N" comment, so they yielded 1 and 0 pairs; that plan was
replaced before any results were seen.

At most ``MAX_PER_REPO`` candidates per repository (newest first), to keep the
number of API requests manageable without a token.

Phase 6 expansion (``--expand``)
--------------------------------
More candidates are found with the search API: issues closed with GitHub's
"duplicate" reason or carrying the repository's duplicate label, newest first,
at most ``EXPANSION_PER_REPO`` per repository. Only repositories whose Phase 4
candidates named the original in a comment at least 30% of the time are
searched (``EXPANSION_REPOS``; tauri-apps/tauri yielded 1 of 15 and pydantic
0 of 22, so they are skipped). The ground-truth rule is unchanged: a pair
exists only if a comment names the original. Each pair records its ``cohort``
("phase4" or "phase6"), so the Phase 4 evaluation stays reproducible. The
development / test split is **frozen per repository** (``FROZEN_SPLITS``, the
Phase 4 assignment): new pairs inherit their repository's split, so no test
repository's pairs can inform development choices.

Licensing: issue text stays in git-ignored ``data/duplicates_raw/`` and
``data/duplicate_pairs.jsonl``; only ``data/duplicate_manifest.csv`` (numbers,
split, URLs — no text) is committed.

Usage:
    python -m scripts.fetch_duplicate_dataset     # resumable; cached requests are reused
    python -m scripts.fetch_duplicate_dataset --max-wait 3600

Uses the core REST API (60 requests/hour anonymously, 5,000 with
``GITHUB_TOKEN`` set in the environment — never pass a token on the command
line or commit it). On a rate limit the script checks ``/rate_limit`` (free):
if requests are in fact available it retries shortly; otherwise it waits for
the reset, but never longer than ``--max-wait`` seconds in total. If it stops
early, every response fetched so far stays cached in ``data/duplicates_raw/``
and re-running the command resumes from there.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = [ROOT / "data" / "raw", ROOT / "data" / "priority_raw"]
RAW_DIR = ROOT / "data" / "duplicates_raw"
PAIRS_FILE = ROOT / "data" / "duplicate_pairs.jsonl"
MANIFEST_FILE = ROOT / "data" / "duplicate_manifest.csv"

MAX_PER_REPO = 15
SEARCH_CUTOFF = "2026-09-01"

# Phase 4 split, frozen (see module docstring); new pairs inherit it.
FROZEN_SPLITS = {
    "microsoft/terminal": "dev",
    "python-poetry/poetry": "dev",
    "denoland/deno": "dev",
    "rust-lang/rust": "dev",
    "microsoft/TypeScript": "dev",
    "tauri-apps/tauri": "dev",
    "pandas-dev/pandas": "test",
    "pypa/pip": "test",
    "psf/black": "test",
    "kubernetes/kubernetes": "test",
    "pytest-dev/pytest": "test",
}
EXPANSION_PER_REPO = 25
# repository -> its duplicate label (None: search by close reason only).
EXPANSION_REPOS: dict[str, str | None] = {
    "microsoft/terminal": "Resolution-Duplicate",
    "python-poetry/poetry": "status/duplicate",
    "denoland/deno": "duplicate",
    "rust-lang/rust": None,
    "microsoft/TypeScript": "Duplicate",
    "pandas-dev/pandas": "Duplicate Report",
    "pypa/pip": "resolution: duplicate",
    "psf/black": "R: duplicate",
    "kubernetes/kubernetes": "triage/duplicate",
    "pytest-dev/pytest": "closed as duplicate",
}

_DUP_LABEL = re.compile(r"dup", re.IGNORECASE)


def canonical_number(text: str, repo: str) -> int | None:
    """Issue number named in a 'duplicate of' comment (same repository only)."""
    owner_repo = re.escape(repo)
    patterns = [
        r"(?:^|\s)/dup(?:licate)?\s+#(\d+)",
        rf"\b(?:duplicate|dupe|dup)s?\s+(?:of\s+)?(?:issue\s+)?(?:{owner_repo})?#(\d+)",
        rf"\b(?:duplicate|dupe|dup)s?\s+(?:of\s+)?(?:issue\s+)?https?://github\.com/{owner_repo}/issues/(\d+)",
    ]
    mentions = [
        (match.start(), int(match.group(1)))
        for pattern in patterns
        for match in re.finditer(pattern, text, re.IGNORECASE)
    ]
    return max(mentions)[1] if mentions else None  # the last mention in the text


def cached_issues() -> dict[tuple[str, int], dict]:
    issues: dict[tuple[str, int], dict] = {}
    for directory in SOURCE_DIRS:
        for path in sorted(directory.glob("*.json")):
            for raw in json.loads(path.read_text(encoding="utf-8")):
                if "pull_request" in raw:
                    continue
                repo = raw.get("repository_url", "").split("/repos/")[-1]
                issues[(repo, raw["number"])] = raw
    return issues


def candidates(issues: dict[tuple[str, int], dict]) -> list[tuple[str, int]]:
    """The Phase 4 candidates: cached duplicates of the repositories with a frozen split.

    The cache also holds repositories added later for other evaluations (e.g. the
    Phase 6 priority expansion); they have no split here and are ignored.
    """
    by_repo: dict[str, list[int]] = {}
    for (repo, number), raw in issues.items():
        if repo not in FROZEN_SPLITS:
            continue
        if raw.get("state_reason") == "duplicate" or any(
            _DUP_LABEL.search(lbl["name"]) for lbl in raw.get("labels", [])
        ):
            by_repo.setdefault(repo, []).append(number)
    picked = []
    for repo in sorted(by_repo):
        for number in sorted(by_repo[repo], reverse=True)[:MAX_PER_REPO]:
            picked.append((repo, number))
    return picked


def _search_cache(repo: str, kind: str) -> Path:
    return RAW_DIR / f"{repo.replace('/', '__')}__search_{kind}.json"


def search_expansion(max_wait: int) -> dict[tuple[str, int], dict]:
    """Duplicate-closed or duplicate-labelled issues per repository (search API, cached)."""
    from scripts.fetch_dataset import _search

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "triagebot-dataset"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    found: dict[tuple[str, int], dict] = {}
    with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30) as http:
        for repo, label in EXPANSION_REPOS.items():
            queries = {"reason": f"repo:{repo} is:issue is:closed reason:duplicate"}
            if label:
                queries["label"] = f'repo:{repo} is:issue label:"{label}"'
            for kind, query in queries.items():
                path = _search_cache(repo, kind)
                if not path.exists():
                    data = _search(
                        http, f"{query} created:<{SEARCH_CUTOFF}", per_page=EXPANSION_PER_REPO * 2
                    )
                    path.write_text(json.dumps(data["items"]), encoding="utf-8")
                    time.sleep(2.1 if token else 6.5)
                for raw in json.loads(path.read_text(encoding="utf-8")):
                    if "pull_request" not in raw:
                        found[(repo, raw["number"])] = raw
    return found


def expansion_candidates(
    found: dict[tuple[str, int], dict], already: set[tuple[str, int]]
) -> list[tuple[str, int]]:
    """Newest ``EXPANSION_PER_REPO`` new candidates per repository, interleaved round-robin
    so that a run stopped early by the rate limit still covers every repository."""
    per_repo = {
        repo: sorted((n for r, n in found if r == repo and (r, n) not in already), reverse=True)[
            :EXPANSION_PER_REPO
        ]
        for repo in EXPANSION_REPOS
    }
    order = []
    for i in range(EXPANSION_PER_REPO):
        order += [(repo, nums[i]) for repo, nums in per_repo.items() if i < len(nums)]
    return order


class WaitLimitReached(Exception):
    """Waiting for the GitHub rate limit would exceed ``--max-wait``."""


class Client:
    """GET with an on-disk cache and bounded rate-limit waiting."""

    def __init__(self, max_wait: int = 300) -> None:
        self.max_wait = max_wait
        self.waited = 0
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "triagebot-dataset"}
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30)

    def get(self, url: str, cache: Path) -> object | None:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        while True:
            resp = self.http.get(url)
            if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
                self._wait_for_quota(int(resp.headers.get("x-ratelimit-reset", time.time() + 60)))
                continue
            if resp.status_code in (404, 410):
                data = None
            else:
                resp.raise_for_status()
                data = resp.json()
            cache.write_text(json.dumps(data), encoding="utf-8")
            return data

    def _wait_for_quota(self, reset: int) -> None:
        # GitHub occasionally reports "remaining: 0" while quota is available;
        # /rate_limit does not count against the limit, so check before sleeping.
        status = self.http.get("/rate_limit")
        remaining = (
            status.json().get("resources", {}).get("core", {}).get("remaining", 0)
            if status.is_success
            else 0
        )
        wait = 10 if remaining > 0 else max(1, reset - int(time.time()) + 2)
        if self.waited + wait > self.max_wait:
            raise WaitLimitReached(f"would need to wait {wait}s for the GitHub rate limit")
        print(f"  rate limited (core remaining {remaining}); waiting {wait}s", flush=True)
        time.sleep(wait)
        self.waited += wait


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch real duplicate issue pairs.")
    parser.add_argument(
        "--max-wait",
        type=int,
        default=300,
        help="maximum total seconds to wait for rate limits (default 300)",
    )
    parser.add_argument(
        "--expand",
        action="store_true",
        help="add the Phase 6 search-based candidates (see module docstring)",
    )
    parser.add_argument(
        "--max-new",
        type=int,
        default=None,
        help="process only the first N expansion candidates (round-robin order)",
    )
    args = parser.parse_args()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    issues = cached_issues()
    picked = [(repo, number, "phase4") for repo, number in candidates(issues)]
    if args.expand:
        found = search_expansion(args.max_wait)
        new = expansion_candidates(found, {(r, n) for r, n, _ in picked})[: args.max_new]
        for key in new:
            issues.setdefault(key, found[key])
        picked += [(repo, number, "phase6") for repo, number in new]
    print(f"{len(picked)} duplicate candidates")
    client = Client(max_wait=args.max_wait)
    try:
        pairs, skipped = collect(client, issues, picked)
    except WaitLimitReached as exc:
        cached = len(list(RAW_DIR.glob("*.json")))
        raise SystemExit(
            f"Stopped early: {exc}. {cached} responses are cached in "
            f"{RAW_DIR.relative_to(ROOT)}; re-run later (or set GITHUB_TOKEN) "
            "to resume. No pairs file was written."
        ) from None
    write(pairs, skipped)


def collect(client: Client, issues: dict, picked: list) -> tuple[list[dict], dict]:
    pairs, skipped = [], {"no canonical comment": 0, "canonical missing or PR": 0}
    for repo, number, cohort in picked:
        slug = repo.replace("/", "__")
        timeline = (
            client.get(
                f"/repos/{repo}/issues/{number}/timeline?per_page=100",
                RAW_DIR / f"{slug}__{number}__timeline.json",
            )
            or []
        )
        text = "\n".join(
            ev.get("body") or ""
            for ev in timeline
            if isinstance(ev, dict) and ev.get("event") == "commented"
        )
        target = canonical_number(text, repo)
        if target is None or target == number:
            skipped["no canonical comment"] += 1
            continue
        original = issues.get((repo, target)) or client.get(
            f"/repos/{repo}/issues/{target}", RAW_DIR / f"{slug}__{target}__issue.json"
        )
        if not original or "pull_request" in original:
            skipped["canonical missing or PR"] += 1
            continue
        dup = issues[(repo, number)]
        pairs.append(
            {
                "repo": repo,
                "cohort": cohort,
                "duplicate": {
                    "number": number,
                    "title": dup.get("title") or "",
                    "body": dup.get("body") or "",
                    "html_url": dup.get("html_url"),
                },
                "canonical": {
                    "number": target,
                    "title": original.get("title") or "",
                    "body": original.get("body") or "",
                    "html_url": original.get("html_url"),
                },
            }
        )
        print(f"{repo:24} #{number} -> #{target}", flush=True)
    return pairs, skipped


def assign_splits(pairs: list[dict]) -> None:
    """Each pair takes its repository's frozen split (``FROZEN_SPLITS``).

    Phase 4 derived this mapping once from pair counts (repositories sorted by
    count, alternately development and test); recomputing it as pairs are added
    would move repositories between splits, so it is fixed instead.
    """
    for p in pairs:
        if p["repo"] not in FROZEN_SPLITS:
            raise ValueError(f"no split assigned for {p['repo']}")
        p["split"] = FROZEN_SPLITS[p["repo"]]


def write(pairs: list[dict], skipped: dict) -> None:
    assign_splits(pairs)
    with PAIRS_FILE.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")
    with MANIFEST_FILE.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["repo", "split", "cohort", "duplicate", "canonical", "duplicate_url", "canonical_url"]
        )
        for p in pairs:
            writer.writerow(
                [
                    p["repo"],
                    p["split"],
                    p.get("cohort", "phase4"),
                    p["duplicate"]["number"],
                    p["canonical"]["number"],
                    p["duplicate"]["html_url"],
                    p["canonical"]["html_url"],
                ]
            )
    print(f"{len(pairs)} pairs written; skipped: {skipped}")


if __name__ == "__main__":
    main()
