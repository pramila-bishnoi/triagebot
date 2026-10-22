"""Configuration parsing: invalid values fall back safely and are reported in the log."""

from __future__ import annotations

import logging

import pytest

from app.config import get_settings, warn_invalid
from app.pr_insights import thresholds_from_env


@pytest.fixture(autouse=True)
def _fresh_warnings():
    warn_invalid.cache_clear()
    yield
    warn_invalid.cache_clear()


def test_defaults(monkeypatch):
    for name in ("GITHUB_TIMEOUT", "TRIAGEBOT_ML_THRESHOLD", "TRIAGEBOT_DUP_THRESHOLD"):
        monkeypatch.delenv(name, raising=False)
    settings = get_settings()
    assert (settings.github_timeout, settings.ml_threshold, settings.duplicate_threshold) == (
        10.0,
        0.5,
        None,
    )


@pytest.mark.parametrize(
    ("name", "raw", "field", "expected"),
    [
        ("TRIAGEBOT_ML_THRESHOLD", "abc", "ml_threshold", 0.5),
        ("TRIAGEBOT_ML_THRESHOLD", "1.7", "ml_threshold", 1.0),
        ("TRIAGEBOT_DUP_THRESHOLD", "0.01", "duplicate_threshold", 0.05),
        ("TRIAGEBOT_DUP_THRESHOLD", "lots", "duplicate_threshold", None),
        ("TRIAGEBOT_DUP_THRESHOLD", "-3", "duplicate_threshold", None),
        ("GITHUB_TIMEOUT", "0", "github_timeout", 10.0),
        ("GITHUB_TIMEOUT", "soon", "github_timeout", 10.0),
    ],
)
def test_invalid_values_fall_back_with_a_warning(monkeypatch, caplog, name, raw, field, expected):
    monkeypatch.setenv(name, raw)
    with caplog.at_level(logging.WARNING, logger="app.config"):
        assert getattr(get_settings(), field) == expected
    assert f"ignoring invalid {name}" in caplog.text


def test_valid_values_do_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("TRIAGEBOT_ML_THRESHOLD", "0.7")
    monkeypatch.setenv("TRIAGEBOT_DUP_THRESHOLD", "0.3")
    with caplog.at_level(logging.WARNING):
        settings = get_settings()
    assert (settings.ml_threshold, settings.duplicate_threshold) == (0.7, 0.3)
    assert caplog.text == ""


def test_warning_is_logged_once_per_value(monkeypatch, caplog):
    monkeypatch.setenv("TRIAGEBOT_ML_THRESHOLD", "abc")
    with caplog.at_level(logging.WARNING, logger="app.config"):
        for _ in range(5):
            get_settings()
    assert caplog.text.count("TRIAGEBOT_ML_THRESHOLD") == 1


def test_pr_threshold_warning(monkeypatch, caplog):
    monkeypatch.setenv("TRIAGEBOT_PR_STALE_DAYS", "-2")
    with caplog.at_level(logging.WARNING, logger="app.config"):
        assert thresholds_from_env().stale_days == 30
    assert "TRIAGEBOT_PR_STALE_DAYS" in caplog.text


def test_token_is_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("GITHUB_TOKEN", "  fake-token-for-tests  ")
    monkeypatch.setenv("TRIAGEBOT_ML_THRESHOLD", "abc")  # forces a warning to be logged
    with caplog.at_level(logging.DEBUG):
        assert get_settings().github_token == "fake-token-for-tests"
    assert "TRIAGEBOT_ML_THRESHOLD" in caplog.text
    assert "fake-token-for-tests" not in caplog.text
