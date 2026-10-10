"""Shared types and settings for the assist backends."""

import logging
import re
from typing import Literal

from pydantic import BaseModel

from clipforage import config

log = logging.getLogger(__name__)

BackendId = Literal["claude_cli", "anthropic", "openai"]
BACKENDS: tuple[str, ...] = ("claude_cli", "anthropic", "openai")
BACKEND_CHOICES: tuple[str, ...] = ("auto", *BACKENDS)
DEFAULT_TIMEOUT_S = 90.0
DISABLED_MESSAGE = "Assist is disabled; enable it in Settings or set [assist] enabled = true"
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes"})
_AUTH_RE = re.compile(
    r"(not logged in|\blog ?in\b|login|\bauth|oauth|\b401\b|invalid api key|unauthori[sz]ed)",
    re.IGNORECASE,
)
_CLAUDE_MODEL_RE = re.compile(r"^(claude|opus|sonnet|haiku)", re.IGNORECASE)
_OPENAI_MODEL_RE = re.compile(r"^(gpt|o\d|chatgpt)", re.IGNORECASE)


class AssistError(Exception):
    """An assist backend is unavailable, failed, or returned an unusable answer."""


class AssistDisabledError(AssistError):
    """Assist is switched off (``[assist] enabled`` is not true); no backend was contacted."""


class AssistUnavailableError(AssistError):
    """No usable backend: none available (``auto``) or the chosen one is unavailable."""


def looks_like_auth_error(message: str) -> bool:
    """True when a backend error reads like a login / auth / 401 / invalid-key failure."""
    return bool(_AUTH_RE.search(message))


class BackendStatus(BaseModel):
    """Whether an assist backend can be used right now, and why (or why not)."""

    id: str
    available: bool
    detail: str
    model: str | None = None


def configured_enabled() -> bool:
    """Return ``[assist] enabled`` (env ``CLIPFORAGE_ASSIST_ENABLED``); off unless truthy.

    Truthy: TOML ``true`` or the strings ``1`` / ``true`` / ``yes`` (any case).
    """
    env = config._env("ASSIST_ENABLED")
    if env is not None:
        return env.strip().lower() in _TRUTHY
    try:
        table = config.load_config().get("assist", {})
    except Exception:  # noqa: BLE001 - a malformed config never enables assist
        return False
    value = table.get("enabled") if isinstance(table, dict) else None
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


def configured_backend() -> str:
    """Return ``[assist] backend`` (env ``CLIPFORAGE_ASSIST_BACKEND``), default ``auto``."""
    return config.get_setting("assist", "backend", env="ASSIST_BACKEND") or "auto"


def model_matches_backend(model: str, backend: str) -> bool:
    """False when ``model`` clearly belongs to another family than ``backend`` (a ``claude*``
    model on openai, a ``gpt*`` / ``o<digit>*`` model on anthropic or claude_cli); other names
    are passed through."""
    if backend == "openai":
        return not _CLAUDE_MODEL_RE.search(model)
    if backend in ("anthropic", "claude_cli"):
        return not _OPENAI_MODEL_RE.search(model)
    return True


def configured_model(default: str, backend: str | None = None) -> str:
    """Return ``[assist] model`` (env ``CLIPFORAGE_ASSIST_MODEL``), else the backend default.

    With ``backend``, a per-backend ``[assist] <backend>_model`` (env
    ``CLIPFORAGE_ASSIST_<BACKEND>_MODEL``) wins and is returned as-is. Otherwise a shared model
    from another backend family is ignored (with a warning) and ``default`` is used instead.
    """
    if backend is not None:
        per_backend = config.get_setting(
            "assist", f"{backend}_model", env=f"ASSIST_{backend.upper()}_MODEL"
        )
        if per_backend:
            return per_backend
    model = config.get_setting("assist", "model", env="ASSIST_MODEL")
    if not model:
        return default
    if backend is not None and not model_matches_backend(model, backend):
        log.warning(
            "ignoring [assist] model %r for the %s backend (another model family); using %s",
            model,
            backend,
            default,
        )
        return default
    return model


def configured_timeout() -> float:
    """Return ``[assist] timeout_s`` in seconds (default 90); invalid values give the default."""
    value = config.get_setting("assist", "timeout_s", env="ASSIST_TIMEOUT_S")
    try:
        timeout = float(value) if value else DEFAULT_TIMEOUT_S
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return timeout if timeout > 0 else DEFAULT_TIMEOUT_S
