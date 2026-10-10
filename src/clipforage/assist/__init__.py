"""Optional LLM search assist: turn a half-remembered description into search queries.

This is search assistance, not generation: the model only names the thing and suggests
queries. Assist is off unless ``[assist] enabled = true`` (or ``CLIPFORAGE_ASSIST_ENABLED``);
backends are only called from ``describe``, i.e. when the user asks. Backends: the local Claude
Code CLI (reuses its login), the Anthropic SDK (key or ``ant auth login`` profile), or OpenAI
with a user-supplied key. Nothing is shipped.
"""

import logging
from types import ModuleType

from clipforage.assist import anthropic_api, claude_cli, openai_api
from clipforage.assist.base import (
    BACKEND_CHOICES,
    BACKENDS,
    DISABLED_MESSAGE,
    AssistDisabledError,
    AssistError,
    AssistUnavailableError,
    BackendStatus,
    configured_backend,
    configured_enabled,
    configured_model,
    configured_timeout,
    looks_like_auth_error,
)
from clipforage.assist.prompt import build_prompt, parse_result
from clipforage.models import AssistResult, Kind

__all__ = [
    "BACKEND_CHOICES",
    "DISABLED_MESSAGE",
    "ENABLE_HINTS",
    "AssistDisabledError",
    "AssistError",
    "AssistUnavailableError",
    "BackendStatus",
    "auto_pick",
    "available_backends",
    "configured_backend",
    "configured_enabled",
    "describe",
]

log = logging.getLogger(__name__)

_MODULES: dict[str, ModuleType] = {
    claude_cli.ID: claude_cli,
    anthropic_api.ID: anthropic_api,
    openai_api.ID: openai_api,
}

ENABLE_HINTS = (
    "To enable assist, use one of:\n"
    "  - Claude Code: install it (https://claude.com/claude-code) and run `claude` once to log in\n"
    "  - Anthropic API: pip install clipforage[assist], then `ant auth login` "
    "or set CLIPFORAGE_ANTHROPIC_KEY\n"
    "  - OpenAI API: set CLIPFORAGE_OPENAI_KEY (bring your own key; OpenAI has no sign-in "
    "for third-party apps)"
)


def available_backends() -> list[BackendStatus]:
    """Return the status of every backend, in ``auto`` preference order."""
    return [_MODULES[backend_id].status() for backend_id in BACKENDS]


def auto_pick(statuses: list[BackendStatus] | None = None) -> str | None:
    """The backend id ``auto`` would try first right now, or None if none is available."""
    for status in statuses if statuses is not None else available_backends():
        if status.available:
            return status.id
    return None


def _candidates(backend: str) -> list[BackendStatus]:
    """Backends to try, in order: every available one for ``auto``, else just ``backend``.

    Raises ``AssistUnavailableError`` when there is none, ``AssistError`` for an unknown id.
    """
    if backend == "auto":
        statuses = available_backends()
        usable = [status for status in statuses if status.available]
        if usable:
            return usable
        details = "\n".join(f"  {s.id}: {s.detail}" for s in statuses)
        raise AssistUnavailableError(f"no assist backend available:\n{details}\n{ENABLE_HINTS}")
    if backend not in _MODULES:
        valid = ", ".join(BACKEND_CHOICES)
        raise AssistError(f"unknown assist backend {backend!r}; choose one of: {valid}")
    status = _MODULES[backend].status()
    if not status.available:
        raise AssistUnavailableError(
            f"{backend} backend unavailable: {status.detail}\n{ENABLE_HINTS}"
        )
    return [status]


def _run_backend(status: BackendStatus, system: str, user: str) -> AssistResult:
    module = _MODULES[status.id]
    model = configured_model(module.DEFAULT_MODEL, status.id)
    raw, cost_usd, model_used = module.run(
        system, user, model=model, timeout_s=configured_timeout()
    )
    result = parse_result(raw, status.id, model_used or model)
    if cost_usd is not None:
        log.info("assist %s (%s) cost $%.4f", status.id, result.model, cost_usd)
    return result.model_copy(update={"cost_usd": cost_usd})


def describe(
    text: str,
    *,
    kind: Kind | None = None,
    style: str | None = None,
    backend: str | None = None,
) -> AssistResult:
    """Turn a free-text description into search queries, names and a rights note.

    ``backend`` overrides ``[assist] backend`` (``auto`` by default). With ``auto``, a backend
    failing with a login / auth error falls through to the next available one; any other
    failure is raised. Raises ``AssistDisabledError`` unless assist is enabled (nothing is
    contacted then), ``AssistUnavailableError`` when no backend is available, and
    ``AssistError`` when a backend fails. The reported cost (if any) is logged and returned on
    the result.
    """
    if not configured_enabled():
        raise AssistDisabledError(DISABLED_MESSAGE)
    if not text.strip():
        raise AssistError("describe needs some text")
    chosen = backend or configured_backend()
    candidates = _candidates(chosen)
    system, user = build_prompt(text, kind, style)
    if chosen != "auto":
        return _run_backend(candidates[0], system, user)
    failures: list[str] = []
    for status in candidates:
        try:
            return _run_backend(status, system, user)
        except AssistError as exc:
            if not looks_like_auth_error(str(exc)):
                raise
            log.warning("assist %s not signed in, trying the next backend: %s", status.id, exc)
            failures.append(f"  {status.id}: {exc}")
    summary = "\n".join(failures)
    raise AssistError(f"every available assist backend failed to sign in:\n{summary}")
