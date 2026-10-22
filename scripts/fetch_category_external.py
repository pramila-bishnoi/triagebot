"""Fetch an *external* test set for the category classifier (Phase 6).

Why
---
The model's held-out test split (``scripts/train_classifier.py``) comes from
the same ten repositories as its training data, and the leave-one-repository-
out numbers were already used while developing the model. This set contains
only repositories the model has never seen and that played no part in any
modelling decision, so it is a fresh check of generalisation.

Selection rule (applied to label *counts* from the search API, before any
issue text was fetched): not in the training data; an explicit label for each
of bug / feature / documentation / question whose meaning is unambiguous; at
least 30 issues created before ``CUTOFF`` per class. Probed and excluded:
psf/requests ("Question/Not a bug" mixes two meanings), rust-lang/cargo (15
questions), astral-sh/ruff (no feature label), pypa/pipenv and nushell/nushell
(labels absent).

Sampling: the newest ``PER_LABEL`` issues per (repository, class) created
before ``CUTOFF``; pull requests and issues carrying labels of several classes
are dropped, exactly as for the training data.

Licensing: as for the training data, issue text stays in git-ignored
``data/category_external_raw/`` and ``data/category_external.jsonl``; only
``data/category_external_manifest.csv`` (repository, number, class, URL) is
committed.

Usage:
    python -m scripts.fetch_category_external            # uses cached pages
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path

import httpx

from scripts.fetch_dataset import CUTOFF, ROOT, _search

RAW_DIR = ROOT / "data" / "category_external_raw"
ISSUES_FILE = ROOT / "data" / "category_external.jsonl"
MANIFEST_FILE = ROOT / "data" / "category_external_manifest.csv"
PER_LABEL = 50

# repo -> {class: maintainer label}
REPOS: dict[str, dict[str, str]] = {
    "astral-sh/uv": {
        "bug": "bug",
        "feature": "enhancement",
        "documentation": "documentation",
        "question": "question",
    },
    "jupyterlab/jupyterlab": {
        "bug": "bug",
        "feature": "enhancement",
        "documentation": "documentation",
        "question": "question",
    },
    "matplotlib/matplotlib": {
        "bug": "status: confirmed bug",
        "feature": "New feature",
        "documentation": "Documentation",
        "question": "Community support",
    },
    "numpy/numpy": {
        "bug": "00 - Bug",
        "feature": "01 - Enhancement",
        "documentation": "04 - Documentation",
        "question": "33 - Question",
    },
    "python/mypy": {
        "bug": "bug",
        "feature": "feature",
        "documentation": "documentation",
        "question": "question",
    },
    "scikit-learn/scikit-learn": {
        "bug": "Bug",
        "feature": "New Feature",
        "documentation": "Documentation",
        "question": "Question",
    },
    "sphinx-doc/sphinx": {
        "bug": "type:bug",
        "feature": "type:enhancement",
        "documentation": "type:docs",
        "question": "type:question",
    },
    "streamlit/streamlit": {
        "bug": "type:bug",
        "feature": "type:enhancement",
        "documentation": "type:docs",
        "question": "type:question",
    },
}


def _cache_path(repo: str, category: str) -> Path:
    return RAW_DIR / f"{repo.replace('/', '__')}__{category}.json"


def fetch(refresh: bool = False) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "triagebot-dataset"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    pause = 2.1 if token else 6.5
    with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30) as client:
        for repo, labels in REPOS.items():
            for category, label in labels.items():
                path = _cache_path(repo, category)
                if path.exists() and not refresh:
                    continue
                query = f'repo:{repo} is:issue label:"{label}" created:<{CUTOFF}'
                data = _search(client, query, per_page=PER_LABEL)
                path.write_text(json.dumps(data["items"]), encoding="utf-8")
                print(f"{repo:28} {category:13} {len(data['items']):3} issues", flush=True)
                time.sleep(pause)


def build() -> list[dict]:
    records: dict[tuple[str, int], dict | None] = {}
    dropped = 0
    for repo, labels in REPOS.items():
        to_class = {label.lower(): cat for cat, label in labels.items()}
        for category in labels:
            for raw in json.loads(_cache_path(repo, category).read_text(encoding="utf-8")):
                if "pull_request" in raw:
                    continue
                names = [lbl["name"] for lbl in raw.get("labels", [])]
                classes = {to_class[n.lower()] for n in names if n.lower() in to_class}
                key = (repo, raw["number"])
                if len(classes) != 1:
                    if records.get(key, "unset") is not None:
                        dropped += 1
                    records[key] = None
                    continue
                if key in records:
                    continue
                records[key] = {
                    "repo": repo,
                    "number": raw["number"],
                    "category": classes.pop(),
                    "title": raw.get("title") or "",
                    "body": raw.get("body") or "",
                    "labels": names,
                    "created_at": raw.get("created_at"),
                    "html_url": raw.get("html_url"),
                }
    rows = sorted((r for r in records.values() if r), key=lambda r: (r["repo"], r["number"]))
    print(f"kept {len(rows)} issues; dropped {dropped} with labels of several classes")
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
    parser = argparse.ArgumentParser(description="Fetch the external category test set.")
    parser.add_argument("--refresh", action="store_true", help="ignore cached pages")
    fetch(refresh=parser.parse_args().refresh)
    write(build())


if __name__ == "__main__":
    main()
