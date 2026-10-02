"""Runtime configuration read from environment variables.

Values are read at call time (not import time) so tests can override them with
``monkeypatch.setenv``. Secrets such as ``GITHUB_TOKEN`` are only ever read
from the environment and never logged or returned by the API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "models", "issue_classifier"
)
DEFAULT_ML_THRESHOLD = 0.5


@dataclass(frozen=True)
class Settings:
    github_api_url: str = DEFAULT_GITHUB_API_URL
    github_token: str | None = None
    github_timeout: float = 10.0
    ml_enabled: bool = True
    model_path: str = DEFAULT_MODEL_PATH
    ml_threshold: float = DEFAULT_ML_THRESHOLD


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def get_settings() -> Settings:
    token = os.environ.get("GITHUB_TOKEN", "").strip() or None
    return Settings(
        github_api_url=os.environ.get("GITHUB_API_URL", DEFAULT_GITHUB_API_URL).rstrip("/"),
        github_token=token,
        github_timeout=_float("GITHUB_TIMEOUT", 10.0),
        ml_enabled=os.environ.get("TRIAGEBOT_ML_ENABLED", "1").strip().lower()
        not in {"0", "false", "no", "off"},
        model_path=os.environ.get("TRIAGEBOT_MODEL_PATH", DEFAULT_MODEL_PATH),
        ml_threshold=min(1.0, max(0.0, _float("TRIAGEBOT_ML_THRESHOLD", DEFAULT_ML_THRESHOLD))),
    )
