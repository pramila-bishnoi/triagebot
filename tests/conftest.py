"""Shared pytest fixtures.

Points TriageBot at a throwaway SQLite file per test so the real
``triagebot.db`` is never touched, replaces GitHub with an in-memory fake, and
provides a FastAPI ``TestClient``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.github_client import GitHubClient, get_github_client
from app.main import app
from app.ml import service as ml_service
from tests.github_fakes import FakeGitHub
from tests.ml_fixtures import train_tiny_model


@pytest.fixture(autouse=True)
def _ml_off_by_default(monkeypatch):
    """Phase 1 tests exercise the rule engine; ML tests opt in via ``ml_model``."""
    monkeypatch.setenv("TRIAGEBOT_ML_ENABLED", "0")
    monkeypatch.delenv("TRIAGEBOT_MODEL_PATH", raising=False)
    monkeypatch.delenv("TRIAGEBOT_ML_THRESHOLD", raising=False)
    ml_service.clear_cache()
    yield
    ml_service.clear_cache()


@pytest.fixture(scope="session")
def tiny_model_path(tmp_path_factory):
    """A small model trained once per session on the synthetic corpus."""
    base = tmp_path_factory.mktemp("model") / "tiny"
    train_tiny_model(base)
    return base


@pytest.fixture()
def ml_model(tiny_model_path, monkeypatch):
    """Enable ML using the tiny test model; returns the model base path."""
    monkeypatch.setenv("TRIAGEBOT_ML_ENABLED", "1")
    monkeypatch.setenv("TRIAGEBOT_MODEL_PATH", str(tiny_model_path))
    ml_service.clear_cache()
    return tiny_model_path


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """An isolated, empty database path (the DB path is read at call time)."""
    path = tmp_path / "test.db"
    monkeypatch.setenv("TRIAGEBOT_DB", str(path))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return path


@pytest.fixture()
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture()
def client(db_path, fake_github) -> TestClient:
    """A TestClient backed by a temporary database and the fake GitHub API."""

    def _fake_client():
        with GitHubClient(transport=fake_github.transport()) as gh:
            yield gh

    app.dependency_overrides[get_github_client] = _fake_client
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
