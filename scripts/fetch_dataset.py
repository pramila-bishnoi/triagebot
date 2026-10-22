"""Fetch the labelled issue dataset used to train the category classifier.

Source
------
Public GitHub issues from ten open-source projects whose maintainers apply a
category label (bug / feature / documentation / question). The maintainer's
label is the ground truth. Only issues (never pull requests) created before
``CUTOFF`` are fetched, so re-running the script yields the same set of issues
unless maintainers later relabel or delete them.

Licensing
---------
The projects' *code* is MIT, BSD-3-Clause or Apache-2.0 licensed (see
``REPOS``). Issue *text* belongs to its authors and is accessed through the
public GitHub API under GitHub's Terms of Service. For that reason the raw
text is stored only locally under ``data/raw/`` and ``data/issues.jsonl``
(both git-ignored) and is never redistributed; the repository only contains
``data/dataset_manifest.csv`` (repo, issue number, label, URL — no text) so
anyone can audit or re-fetch exactly which issues were used.

Usage
-----
    python -m scripts.fetch_dataset            # uses cached pages in data/raw/
    python -m scripts.fetch_dataset --refresh  # re-download everything

Uses GitHub's search API (10 requests/min anonymously, 30/min with
``GITHUB_TOKEN``); the script paces itself and waits out rate limits.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
ISSUES_FILE = DATA_DIR / "issues.jsonl"
MANIFEST_FILE = DATA_DIR / "dataset_manifest.csv"

CUTOFF = "2026-09-01"  # only issues created before this date
PER_LABEL = 100  # newest N issues per (repo, category) — one search page

# repo -> (code licence, {category: maintainer label})
REPOS: dict[str, tuple[str, dict[str, str]]] = {
    "pandas-dev/pandas": (
        "BSD-3-Clause",
        {
            "bug": "Bug",
            "feature": "Enhancement",
            "documentation": "Docs",
            "question": "Usage Question",
        },
    ),
    "pytest-dev/pytest": (
        "MIT",
        {
            "bug": "type: bug",
            "feature": "type: enhancement",
            "documentation": "type: docs",
            "question": "type: question",
        },
    ),
    "microsoft/terminal": (
        "MIT",
        {
            "bug": "Issue-Bug",
            "feature": "Issue-Feature",
            "documentation": "Issue-Docs",
            "question": "Issue-Question",
        },
    ),
    "microsoft/TypeScript": (
        "Apache-2.0",
        {"bug": "Bug", "feature": "Suggestion", "documentation": "Docs", "question": "Question"},
    ),
    "python-poetry/poetry": (
        "MIT",
        {
            "bug": "kind/bug",
            "feature": "kind/feature",
            "documentation": "area/docs",
            "question": "kind/question",
        },
    ),
    "pypa/pip": (
        "MIT",
        {
            "bug": "type: bug",
            "feature": "type: enhancement",
            "documentation": "type: docs",
            "question": "type: support",
        },
    ),
    "psf/black": (
        "MIT",
        {
            "bug": "T: bug",
            "feature": "T: enhancement",
            "documentation": "T: documentation",
            "question": "T: user support",
        },
    ),
    "tauri-apps/tauri": (
        "Apache-2.0 OR MIT",
        {
            "bug": "type: bug",
            "feature": "type: feature request",
            "documentation": "type: documentation",
            "question": "type: question",
        },
    ),
    "pydantic/pydantic": (
        "MIT",
        {
            "bug": "bug V2",
            "feature": "feature request",
            "documentation": "documentation",
            "question": "question",
        },
    ),
    "denoland/deno": (
        "MIT",
        {"bug": "bug", "feature": "feat", "documentation": "docs", "question": "question"},
    ),
}


def _cache_path(repo: str, category: str) -> Path:
    return RAW_DIR / f"{repo.replace('/', '__')}__{category}.json"


def _search(client: httpx.Client, query: str, per_page: int = PER_LABEL) -> dict:
    """One search request, waiting out rate limits instead of failing."""
    while True:
        resp = client.get(
            "/search/issues",
            params={"q": query, "sort": "created", "order": "desc", "per_page": per_page},
        )
        if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            reset = int(resp.headers.get("x-ratelimit-reset", time.time() + 60))
            wait = max(1, reset - int(time.time()) + 1)
            print(f"  rate limited; waiting {wait}s", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()


def fetch(refresh: bool = False) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "triagebot-dataset"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    pause = 2.1 if token else 6.5  # stay under 30 / 10 search requests per minute

    with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30) as client:
        for repo, (_licence, labels) in REPOS.items():
            for category, label in labels.items():
                path = _cache_path(repo, category)
                if path.exists() and not refresh:
                    continue
                query = f'repo:{repo} is:issue label:"{label}" created:<{CUTOFF}'
                data = _search(client, query)
                path.write_text(json.dumps(data["items"]), encoding="utf-8")
                print(f"{repo:24} {category:13} {len(data['items']):4} issues", flush=True)
                time.sleep(pause)


def build() -> list[dict]:
    """Merge cached pages into one de-duplicated, unambiguous dataset."""
    records: dict[tuple[str, int], dict] = {}
    dropped_ambiguous = 0
    for repo, (_licence, labels) in REPOS.items():
        label_to_category = {label.lower(): cat for cat, label in labels.items()}
        for category in labels:
            for raw in json.loads(_cache_path(repo, category).read_text(encoding="utf-8")):
                if "pull_request" in raw:
                    continue
                names = [lbl["name"] for lbl in raw.get("labels", [])]
                categories = {
                    label_to_category[n.lower()] for n in names if n.lower() in label_to_category
                }
                key = (repo, raw["number"])
                if len(categories) != 1:
                    if key not in records:
                        dropped_ambiguous += 1
                    records[key] = None  # type: ignore[assignment]
                    continue
                if key in records:
                    continue
                records[key] = {
                    "repo": repo,
                    "number": raw["number"],
                    "category": categories.pop(),
                    "title": raw.get("title") or "",
                    "body": raw.get("body") or "",
                    "labels": names,
                    "created_at": raw.get("created_at"),
                    "html_url": raw.get("html_url"),
                }
    rows = sorted((r for r in records.values() if r), key=lambda r: (r["repo"], r["number"]))
    print(f"kept {len(rows)} issues; dropped {dropped_ambiguous} with several category labels")
    return rows


def write(rows: list[dict]) -> None:
    with ISSUES_FILE.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with MANIFEST_FILE.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["repo", "number", "category", "created_at", "html_url"])
        for row in rows:
            writer.writerow(
                [row["repo"], row["number"], row["category"], row["created_at"], row["html_url"]]
            )
    print(f"wrote {ISSUES_FILE.relative_to(ROOT)} and {MANIFEST_FILE.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--refresh", action="store_true", help="ignore cached pages")
    args = parser.parse_args()
    fetch(refresh=args.refresh)
    write(build())


if __name__ == "__main__":
    main()
