"""API integration, migration, training-script and shipped-model tests for Phase 2."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import repository
from app.database import init_db
from app.ml.model import IssueClassifier
from tests.github_fakes import make_issue, make_pr
from tests.ml_fixtures import TINY_CORPUS, TINY_VECTORIZER

ROOT = Path(__file__).resolve().parent.parent
SHIPPED_MODEL = ROOT / "models" / "issue_classifier"


@pytest.fixture()
def repo_items(fake_github):
    fake_github.add_repo("acme/widgets", [
        make_issue("crash with traceback when saving file", number=1),
        make_issue("typo in tutorial docs page", number=2),
        make_issue("zzzz qqqq", number=3),                       # ML unsure -> rules
        make_issue("whatever", number=4, labels=["question"]),   # maintainer label
        make_pr("fix: crash on save", number=5),                 # PR -> rules
    ])
    return fake_github


def _import(client):
    resp = client.post("/repos/import", json={"repo": "acme/widgets", "state": "all"})
    assert resp.status_code == 200, resp.text
    return {i["number"]: i for i in client.get("/items").json()["items"]}


def test_import_records_category_source(client, repo_items, ml_model):
    items = _import(client)
    assert items[1]["category_source"] == "ml" and items[1]["category"] == "bug"
    assert 0.5 <= items[1]["category_score"] <= 1
    assert items[1]["category_reasons"][0].startswith("ML model: bug")
    assert items[2]["category_source"] == "ml" and items[2]["category"] == "documentation"
    assert items[3]["category_source"] == "rules" and items[3]["category_score"] is None
    assert items[4]["category_source"] == "github_label" and items[4]["category"] == "question"
    assert items[5]["category_source"] == "rules" and items[5]["kind"] == "pr"


def test_import_without_model_matches_phase1(client, repo_items):
    items = _import(client)
    assert {i["category_source"] for i in items.values()} == {"rules"}
    assert items[1]["category"] == "bug"


def test_source_filter(client, repo_items, ml_model):
    _import(client)
    ml = client.get("/items", params={"source": "ml"}).json()
    assert {i["number"] for i in ml["items"]} == {1, 2}
    combined = client.get("/items", params={"source": "rules", "type": "pr"}).json()
    assert [i["number"] for i in combined["items"]] == [5]
    assert client.get("/items", params={"source": "magic"}).status_code == 422


def test_reimport_after_enabling_ml_updates_source(client, repo_items, monkeypatch,
                                                   tiny_model_path):
    assert _import(client)[1]["category_source"] == "rules"
    monkeypatch.setenv("TRIAGEBOT_ML_ENABLED", "1")
    monkeypatch.setenv("TRIAGEBOT_MODEL_PATH", str(tiny_model_path))
    assert _import(client)[1]["category_source"] == "ml"


def test_triage_endpoint_includes_category(client, ml_model):
    data = client.post("/triage", json={"title": "crash with traceback"}).json()
    assert data["severity"] == "high"          # original fields unchanged
    assert data["category"] == "bug"
    assert data["category_source"] == "ml"
    assert data["category_reasons"]


def test_triage_endpoint_without_model(client):
    data = client.post("/triage", json={"title": "Fix typo in README"}).json()
    assert data["severity"] == "low"
    assert data["category"] == "documentation"
    assert data["category_source"] == "rules"


def test_batch_triage_includes_category(client, ml_model):
    data = client.post("/triage/batch", json=[{"title": "crash traceback"},
                                              {"title": "add support for dark mode"}]).json()
    assert [d["category"] for d in data] == ["bug", "feature"]


def test_manual_issues_unchanged(client, ml_model):
    created = client.post("/issues", json={"title": "API returns 500"}).json()
    assert "category" not in created
    assert created["severity"] == "high"


def test_model_endpoint(client, ml_model):
    data = client.get("/model").json()
    assert data["available"] is True
    assert data["threshold"] == 0.5
    assert data["test_macro_f1"] == 1.0


def test_model_endpoint_when_unavailable(client):
    data = client.get("/model").json()
    assert data["available"] is False
    assert "disabled" in data["reason"]
    assert data["test_macro_f1"] is None


def test_model_endpoint_when_model_file_missing(client, monkeypatch, tmp_path):
    monkeypatch.setenv("TRIAGEBOT_ML_ENABLED", "1")
    monkeypatch.setenv("TRIAGEBOT_MODEL_PATH", str(tmp_path / "missing"))
    data = client.get("/model").json()
    assert data["available"] is False
    assert "no trained model" in data["reason"]


def test_model_endpoint_reports_configured_threshold(client, ml_model, monkeypatch):
    monkeypatch.setenv("TRIAGEBOT_ML_THRESHOLD", "0.7")
    assert client.get("/model").json()["threshold"] == 0.7


@pytest.mark.skipif(not SHIPPED_MODEL.with_suffix(".npz").exists(),
                    reason="trained model not present")
def test_model_endpoint_serves_shipped_metrics(client, monkeypatch):
    """The dashboard's status line shows exactly the recorded evaluation numbers."""
    monkeypatch.setenv("TRIAGEBOT_ML_ENABLED", "1")
    meta = json.loads(SHIPPED_MODEL.with_suffix(".json").read_text())
    data = client.get("/model").json()
    assert data["available"] is True
    assert data["test_macro_f1"] == meta["evaluation"]["test"]["macro_f1"]
    assert data["rules_baseline_macro_f1"] == (
        meta["evaluation"]["rules_baseline_test"]["macro_f1"])
    assert data["unseen_repo_macro_f1"] == meta["evaluation"]["leave_one_repo_out_mean_macro_f1"]
    assert data["train_size"] == meta["training"]["train"]


def test_every_item_has_source_fields_for_the_dashboard(client, repo_items, ml_model):
    for item in _import(client).values():
        assert item["category_source"] in ("ml", "rules", "github_label")
        assert (item["category_score"] is not None) == (item["category_source"] == "ml")
        assert item["category_reasons"]


def test_source_filter_combines_with_others_and_paginates(client, repo_items, ml_model):
    _import(client)
    page = client.get("/items", params={"source": "ml", "category": "bug", "limit": 1}).json()
    assert page["total"] == 1 and page["items"][0]["number"] == 1
    assert client.get("/items", params={"source": "github_label", "q": "whatever"}).json()[
        "total"] == 1


def test_dashboard_page_has_phase2_controls(client):
    html = client.get("/").text
    for marker in ('id="fSource"', 'id="modelStatus"', "sourceChip(", 'fetch(path', "/model"):
        assert marker in html


# --- migration ------------------------------------------------------------------


def test_v2_database_upgrades_to_v3(db_path):
    conn = sqlite3.connect(db_path)
    from app.database import MIGRATIONS
    for version, sql in MIGRATIONS[:2]:
        conn.executescript(f"BEGIN;{sql}PRAGMA user_version = {version};COMMIT;")
    conn.execute("INSERT INTO repos (full_name, html_url) VALUES ('a/b', 'https://github.com/a/b')")
    conn.execute(
        "INSERT INTO items (repo_id, number, kind, title, state, html_url, created_at, "
        "updated_at, category, category_confidence, priority) VALUES "
        "(1, 7, 'issue', 'old', 'open', 'u', 't', 't', 'bug', 'medium', 'high')"
    )
    conn.commit()
    conn.close()

    init_db()

    total, items = repository.list_items()
    assert total == 1
    assert items[0]["category_source"] == "rules"
    assert items[0]["category_score"] is None


# --- training script --------------------------------------------------------------


def test_training_script_end_to_end(tmp_path):
    from scripts.train_classifier import run_training

    rows = []
    for repo in ("org/one", "org/two"):
        for category, sentences in TINY_CORPUS.items():
            for n, sentence in enumerate(sentences):
                rows.append({"repo": repo, "number": n, "category": category,
                             "title": f"{sentence} {repo}", "body": ""})
    dataset = tmp_path / "issues.jsonl"
    dataset.write_text("\n".join(json.dumps(r) for r in rows))

    meta = run_training(dataset, tmp_path / "model", tmp_path / "REPORT.md", seed=1,
                        folds=2, grid=(1.0, 10.0), vectorizer_params=TINY_VECTORIZER)

    assert meta["training"]["train"] + meta["training"]["test"] == len(rows)
    assert meta["evaluation"]["test"]["n"] == meta["training"]["test"]
    assert {r["repo"] for r in meta["evaluation"]["leave_one_repo_out"]} == {"org/one",
                                                                            "org/two"}
    report = (tmp_path / "REPORT.md").read_text()
    assert "Confusion matrix" in report and "rule engine" in report
    loaded = IssueClassifier.load(tmp_path / "model")
    assert loaded.metadata["evaluation"]["test"]["macro_f1"] == pytest.approx(
        meta["evaluation"]["test"]["macro_f1"])


def test_training_dedupes_before_split(tmp_path):
    from scripts.train_classifier import dedupe, load_dataset

    path = tmp_path / "d.jsonl"
    rows = [{"repo": "a/b", "number": i, "category": "bug", "title": "Same  Title",
             "body": ""} for i in range(3)]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    kept, removed = dedupe(load_dataset(path))
    assert len(kept) == 1 and removed == 2


def test_training_script_requires_dataset(tmp_path):
    from scripts.train_classifier import load_dataset

    with pytest.raises(SystemExit, match="fetch_dataset"):
        load_dataset(tmp_path / "missing.jsonl")


# --- the shipped model --------------------------------------------------------------


@pytest.mark.skipif(not SHIPPED_MODEL.with_suffix(".npz").exists(),
                    reason="trained model not present")
def test_shipped_model_loads_and_is_sane():
    model = IssueClassifier.load(SHIPPED_MODEL)
    assert sorted(model.classes) == ["bug", "documentation", "feature", "question"]
    ev = model.metadata["evaluation"]
    assert 0 < ev["test"]["macro_f1"] <= 1
    assert ev["test"]["n"] == model.metadata["training"]["test"]
    # Two unambiguous examples (not an accuracy claim — see models/EVALUATION.md).
    assert model.predict("Typo in the installation docs").category == "documentation"
    assert model.predict("Add support for exporting to parquet").category == "feature"
