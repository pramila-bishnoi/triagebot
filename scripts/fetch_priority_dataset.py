"""Fetch a labelled dataset for evaluating *priority* suggestions.

Separate from the category dataset (``scripts/fetch_dataset.py``); the ML model
and its data are not affected.

Source
------
Public GitHub issues from six projects whose maintainers apply explicit
priority labels. The maintainer's label, mapped to high / medium / low by the
per-repository table ``REPOS`` below, is the reference answer. Projects use
different scales, so the mapping is a documented judgement, e.g. Kubernetes'
``priority/important-soon`` ("staffed and worked on currently or very soon")
is treated as high.

Repositories are split **by repository**, fixed before any issue text was
examined: development repos (error analysis and any scoring changes) and
held-out test repos (scored once, at the end).

Sampling: the newest issues created before ``CUTOFF``, up to ``PER_LEVEL`` per
(repo, level), split evenly across that level's labels. Pull requests are
excluded; issues carrying labels of more than one level are dropped.

Licensing: as for the category dataset, issue text belongs to its authors, so
the text stays in git-ignored ``data/priority_raw/`` and
``data/priority_issues.jsonl``; only ``data/priority_manifest.csv`` (repo,
number, level, split, URL — no text) is committed.

Usage:
    python -m scripts.fetch_priority_dataset            # uses cached pages
    python -m scripts.fetch_priority_dataset --refresh
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path

import httpx

from scripts.fetch_dataset import ROOT, _search

RAW_DIR = ROOT / "data" / "priority_raw"
ISSUES_FILE = ROOT / "data" / "priority_issues.jsonl"
MANIFEST_FILE = ROOT / "data" / "priority_manifest.csv"

CUTOFF = "2026-09-01"
PER_LEVEL = 40

# repo -> (split, {label: level}). Split fixed in advance (see module docstring).
REPOS: dict[str, tuple[str, dict[str, str]]] = {
    "microsoft/terminal": (
        "dev",
        {"Priority-1": "high", "Priority-2": "medium", "Priority-3": "low"},
    ),
    "tauri-apps/tauri": (
        "dev",
        {
            "priority: 0 crash": "high",
            "priority: 1 high": "high",
            "priority: 2 medium": "medium",
            "priority: 3 low": "low",
        },
    ),
    "kubernetes/kubernetes": (
        "dev",
        {
            "priority/critical-urgent": "high",
            "priority/important-soon": "high",
            "priority/important-longterm": "medium",
            "priority/backlog": "low",
        },
    ),
    "rust-lang/rust": (
        "test",
        {"P-critical": "high", "P-high": "high", "P-medium": "medium", "P-low": "low"},
    ),
    "ray-project/ray": ("test", {"P0": "high", "P1": "high", "P2": "medium", "P3": "low"}),
    "flutter/flutter": ("test", {"P0": "high", "P1": "high", "P2": "medium", "P3": "low"}),
    # --- Phase 6 expansion (splits "dev2" / "test2") ------------------------------
    # Selection rule, applied to label *counts* from the search API before any issue
    # text was fetched: an explicit ordinal priority or severity scale with at least
    # 40 issues created before CUTOFF at every mapped level. Probed and excluded:
    # angular/angular (20 issues at P0+P1), owncloud/core (35 at p4-low), and
    # grafana/grafana, woocommerce/woocommerce, istio/istio, zulip/zulip (scale
    # labels absent or one level only).
    # Mapping rule (same as above): on a scale of 4+ levels the top two are "high",
    # the third "medium", the fourth "low"; a 3-level scale maps one to one.
    # Split rule: the 15 repositories in case-insensitive name order, alternately
    # dev2 / test2 (starting with dev2), fixed before any text was fetched.
    "ankidroid/Anki-Android": (
        "dev2",
        {"Priority-High": "high", "Priority-Medium": "medium", "Priority-Low": "low"},
    ),
    "apache/airflow": (
        "test2",
        {
            "priority:critical": "high",
            "priority:high": "high",
            "priority:medium": "medium",
            "priority:low": "low",
        },
    ),
    "appsmithorg/appsmith": (
        "dev2",
        {"Critical": "high", "High": "high", "Medium": "medium", "Low": "low"},
    ),
    "Automattic/wp-calypso": (
        "test2",
        {
            "[Pri] BLOCKER": "high",
            "[Pri] High": "high",
            "[Pri] Normal": "medium",
            "[Pri] Low": "low",
        },
    ),
    "aws/aws-cdk": ("dev2", {"p0": "high", "p1": "medium", "p2": "low"}),
    "aws/aws-sdk-js-v3": ("test2", {"p0": "high", "p1": "high", "p2": "medium", "p3": "low"}),
    "cockroachdb/cockroach": (
        "dev2",
        {"P-0": "high", "P-1": "high", "P-2": "medium", "P-3": "low"},
    ),
    "dart-lang/sdk": ("test2", {"P0": "high", "P1": "high", "P2": "medium", "P3": "low"}),
    "dotnet/aspnetcore": (
        "dev2",
        {"Priority:0": "high", "Priority:1": "high", "Priority:2": "medium", "Priority:3": "low"},
    ),
    "elastic/kibana": (
        "test2",
        {
            "impact:critical": "high",
            "impact:high": "high",
            "impact:medium": "medium",
            "impact:low": "low",
        },
    ),
    "kubernetes/minikube": (
        "dev2",
        {
            "priority/critical-urgent": "high",
            "priority/important-soon": "high",
            "priority/important-longterm": "medium",
            "priority/backlog": "low",
        },
    ),
    "MetaMask/metamask-extension": (
        "test2",
        {"Sev1-high": "high", "Sev2-normal": "medium", "Sev3-low": "low"},
    ),
    "open-telemetry/opentelemetry-collector-contrib": (
        "dev2",
        {
            "priority:p0": "high",
            "priority:p1": "high",
            "priority:p2": "medium",
            "priority:p3": "low",
        },
    ),
    "pingcap/tidb": (
        "test2",
        {
            "severity/critical": "high",
            "severity/major": "high",
            "severity/moderate": "medium",
            "severity/minor": "low",
        },
    ),
    "strapi/strapi": (
        "dev2",
        {
            "severity: critical": "high",
            "severity: high": "high",
            "severity: medium": "medium",
            "severity: low": "low",
        },
    ),
}


def _cache_path(repo: str, label: str) -> Path:
    safe = "".join(ch if ch.isalnum() else "_" for ch in label)
    return RAW_DIR / f"{repo.replace('/', '__')}__{safe}.json"


def _per_label(labels: dict[str, str]) -> dict[str, int]:
    """Split the per-level budget evenly across the labels of each level."""
    counts: dict[str, int] = {}
    for level in set(labels.values()):
        names = [name for name, lv in labels.items() if lv == level]
        for name in names:
            counts[name] = PER_LEVEL // len(names)
    return counts


def fetch(refresh: bool = False) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "triagebot-dataset"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    pause = 2.1 if token else 6.5
    with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30) as client:
        for repo, (_split, labels) in REPOS.items():
            for label, n in _per_label(labels).items():
                path = _cache_path(repo, label)
                if path.exists() and not refresh:
                    continue
                query = f'repo:{repo} is:issue label:"{label}" created:<{CUTOFF}'
                data = _search(client, query, per_page=n)
                path.write_text(json.dumps(data["items"]), encoding="utf-8")
                print(f"{repo:24} {label:28} {len(data['items']):3} issues", flush=True)
                time.sleep(pause)


def build() -> list[dict]:
    records: dict[tuple[str, int], dict | None] = {}
    dropped = 0
    for repo, (split, labels) in REPOS.items():
        mapping = {name.lower(): level for name, level in labels.items()}
        for label in labels:
            for raw in json.loads(_cache_path(repo, label).read_text(encoding="utf-8")):
                if "pull_request" in raw:
                    continue
                names = [lbl["name"] for lbl in raw.get("labels", [])]
                levels = {mapping[n.lower()] for n in names if n.lower() in mapping}
                key = (repo, raw["number"])
                if len(levels) != 1:
                    if records.get(key, "unset") is not None:
                        dropped += 1
                    records[key] = None
                    continue
                if key in records:
                    continue
                records[key] = {
                    "repo": repo,
                    "split": split,
                    "number": raw["number"],
                    "level": levels.pop(),
                    "title": raw.get("title") or "",
                    "body": raw.get("body") or "",
                    # the reference label is removed so the scorer can never see it
                    "labels": [n for n in names if n.lower() not in mapping],
                    "reactions": (raw.get("reactions") or {}).get("total_count", 0),
                    "comments": raw.get("comments", 0),
                    "state": raw.get("state"),
                    "created_at": raw.get("created_at"),
                    "html_url": raw.get("html_url"),
                }
    rows = sorted((r for r in records.values() if r), key=lambda r: (r["repo"], r["number"]))
    print(f"kept {len(rows)} issues; dropped {dropped} with labels of several levels")
    return rows


def write(rows: list[dict]) -> None:
    with ISSUES_FILE.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with MANIFEST_FILE.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["repo", "split", "number", "level", "created_at", "html_url"])
        for row in rows:
            writer.writerow(
                [
                    row["repo"],
                    row["split"],
                    row["number"],
                    row["level"],
                    row["created_at"],
                    row["html_url"],
                ]
            )
    print(f"wrote {ISSUES_FILE.relative_to(ROOT)} and {MANIFEST_FILE.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch the priority evaluation dataset.")
    parser.add_argument("--refresh", action="store_true", help="ignore cached pages")
    fetch(refresh=parser.parse_args().refresh)
    write(build())


if __name__ == "__main__":
    main()
