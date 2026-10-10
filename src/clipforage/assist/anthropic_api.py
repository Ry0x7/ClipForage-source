"""Assist backend: the Anthropic Python SDK (optional extra ``clipforage[assist]``).

Credentials: ``CLIPFORAGE_ANTHROPIC_KEY`` / ``[keys] anthropic`` if set, else the SDK's own
resolution (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``, or an ``ant auth login`` profile)
through a zero-arg client. Nothing is shipped.
"""

import importlib.util
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipforage import config
from clipforage.assist.base import AssistError, BackendStatus, configured_model
from clipforage.assist.prompt import RESULT_SCHEMA

ID = "anthropic"
DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 4096
INSTALL_HINT = "python package `anthropic` not installed; pip install clipforage[assist]"

ClientFactory = Callable[[str | None, float], Any]
"""``client_factory(api_key_or_None, timeout_s) -> client`` with ``.messages.create``."""


def _package_installed() -> bool:
    return importlib.util.find_spec("anthropic") is not None


def _profile_dir() -> Path:
    """Return where ``ant auth login`` stores profiles (``ANTHROPIC_CONFIG_DIR`` overrides)."""
    override = os.environ.get("ANTHROPIC_CONFIG_DIR")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if os.name == "nt" and appdata:
        return Path(appdata) / "Anthropic"
    return Path.home() / ".config" / "anthropic"


def _has_profile() -> bool:
    credentials = _profile_dir() / "credentials"
    return credentials.is_dir() and any(credentials.glob("*.json"))


def credential_source() -> str | None:
    """Describe the credential the backend would use, or None if none is found."""
    if config.get_key(ID):
        return "CLIPFORAGE_ANTHROPIC_KEY / [keys] anthropic set"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY set"
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "ANTHROPIC_AUTH_TOKEN set"
    if _has_profile():
        return "`ant auth login` profile found"
    return None


def status() -> BackendStatus:
    """Report whether the SDK is installed and a credential is configured."""
    model = configured_model(DEFAULT_MODEL, ID)
    if not _package_installed():
        return BackendStatus(id=ID, available=False, detail=INSTALL_HINT, model=model)
    source = credential_source()
    if source is None:
        return BackendStatus(
            id=ID,
            available=False,
            detail="no credentials: run `ant auth login`, or set CLIPFORAGE_ANTHROPIC_KEY "
            "or [keys] anthropic",
            model=model,
        )
    return BackendStatus(id=ID, available=True, detail=source, model=model)


def _default_factory(api_key: str | None, timeout_s: float) -> Any:
    import anthropic

    if api_key:
        return anthropic.Anthropic(api_key=api_key, timeout=timeout_s)
    return anthropic.Anthropic(timeout=timeout_s)  # env vars or an `ant auth login` profile


def request_params(system: str, user: str, model: str) -> dict[str, Any]:
    """Return the ``messages.create`` keyword arguments (structured JSON output, low effort)."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {
            "effort": "low",
            "format": {"type": "json_schema", "schema": RESULT_SCHEMA},
        },
    }


def _create(client: Any, params: dict[str, Any]) -> Any:
    try:
        import anthropic
    except ImportError:  # a test client factory without the SDK installed
        return client.messages.create(**params)
    try:
        return client.messages.create(**params)
    except anthropic.AuthenticationError:
        raise AssistError(
            "Anthropic API rejected the credentials: re-run `ant auth login` or check "
            "CLIPFORAGE_ANTHROPIC_KEY / ANTHROPIC_API_KEY"
        ) from None
    except anthropic.APIStatusError as exc:
        raise AssistError(f"Anthropic API error {exc.status_code}: {exc.message}") from None
    except anthropic.APIConnectionError as exc:
        raise AssistError(f"could not reach the Anthropic API: {exc}") from None


def run(
    system: str,
    user: str,
    *,
    model: str,
    timeout_s: float = 90.0,
    client_factory: ClientFactory | None = None,
) -> tuple[str, float | None, str | None]:
    """Run one completion; return ``(result_text, cost_usd, model_used)``.

    The SDK does not report cost, so ``cost_usd`` is always None.
    """
    if client_factory is None:
        if not _package_installed():
            raise AssistError(INSTALL_HINT)
        client_factory = _default_factory
    client = client_factory(config.get_key(ID), timeout_s)
    response = _create(client, request_params(system, user, model))
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        suffix = f" (category: {category})" if category else ""
        raise AssistError(f"Claude declined this request{suffix}; try rephrasing it")
    if stop_reason == "max_tokens":
        raise AssistError("Claude's answer was cut off (max_tokens); try a shorter description")
    text = "".join(
        getattr(block, "text", "")
        for block in getattr(response, "content", []) or []
        if getattr(block, "type", None) == "text"
    )
    if not text.strip():
        raise AssistError("Anthropic API returned no text")
    return text, None, getattr(response, "model", None) or model
