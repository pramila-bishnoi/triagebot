"""Runtime configuration read from environment variables.

Values are read at call time (not import time) so tests can override them with
``monkeypatch.setenv``. Secrets such as ``GITHUB_TOKEN`` are only ever read
from the environment and never logged or returned by the API.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger(__name__)

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
    duplicate_threshold: float | None = None  # None = app.duplicates.DEFAULT_THRESHOLD


@lru_cache(maxsize=64)
def warn_invalid(name: str, value: str, used: object) -> None:
    """Log (once per distinct value) that a setting was ignored or adjusted.

    Settings are read on every request, so an unparsable value must not stop
    the app; it falls back to a safe value and says so in the log. Never call
    this for secrets: the raw value is logged.
    """
    log.warning("ignoring invalid %s=%r; using %r", name, value, used)


def _float(
    name: str, default: float, *, low: float | None = None, high: float | None = None
) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        warn_invalid(name, raw, default)
        return default
    clamped = min(high, value) if high is not None else value
    clamped = max(low, clamped) if low is not None else clamped
    if clamped != value:
        warn_invalid(name, raw, clamped)
    return clamped


def get_settings() -> Settings:
    token = os.environ.get("GITHUB_TOKEN", "").strip() or None
    # Unset or invalid -> None (the evaluated default in app.duplicates).
    duplicate_threshold = _float("TRIAGEBOT_DUP_THRESHOLD", -1.0)
    if duplicate_threshold >= 0:
        duplicate_threshold = _float("TRIAGEBOT_DUP_THRESHOLD", -1.0, low=0.05, high=1.0)
    elif os.environ.get("TRIAGEBOT_DUP_THRESHOLD", "-1").strip() not in ("", "-1"):
        warn_invalid("TRIAGEBOT_DUP_THRESHOLD", os.environ["TRIAGEBOT_DUP_THRESHOLD"], "default")
    timeout = _float("GITHUB_TIMEOUT", 10.0)
    if timeout <= 0:
        warn_invalid("GITHUB_TIMEOUT", str(timeout), 10.0)
        timeout = 10.0
    return Settings(
        github_api_url=os.environ.get("GITHUB_API_URL", DEFAULT_GITHUB_API_URL).rstrip("/"),
        github_token=token,
        github_timeout=timeout,
        ml_enabled=os.environ.get("TRIAGEBOT_ML_ENABLED", "1").strip().lower()
        not in {"0", "false", "no", "off"},
        model_path=os.environ.get("TRIAGEBOT_MODEL_PATH", DEFAULT_MODEL_PATH),
        ml_threshold=_float("TRIAGEBOT_ML_THRESHOLD", DEFAULT_ML_THRESHOLD, low=0.0, high=1.0),
        duplicate_threshold=duplicate_threshold if duplicate_threshold >= 0 else None,
    )
