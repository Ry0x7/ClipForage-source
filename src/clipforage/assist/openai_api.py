"""Assist backend: OpenAI Chat Completions over plain httpx, with a user-supplied API key.

OpenAI offers no sign-in flow for third-party apps calling its API, so this backend needs a
key: ``CLIPFORAGE_OPENAI_KEY`` / ``[keys] openai`` (``OPENAI_API_KEY`` as a fallback).
"""

import os
from typing import Any

import httpx

from clipforage import config
from clipforage.assist.base import AssistError, BackendStatus, configured_model

ID = "openai"
DEFAULT_MODEL = "gpt-4o-mini"
API_URL = "https://api.openai.com/v1/chat/completions"
NO_KEY_HINT = "no key: set CLIPFORAGE_OPENAI_KEY or [keys] openai"


def api_key() -> str | None:
    """Return the OpenAI key: ClipForage config first, then ``OPENAI_API_KEY``."""
    return config.get_key(ID) or os.environ.get("OPENAI_API_KEY") or None


def status() -> BackendStatus:
    """Report whether an OpenAI key is configured."""
    model = configured_model(DEFAULT_MODEL, ID)
    if config.get_key(ID):
        return BackendStatus(
            id=ID, available=True, detail="CLIPFORAGE_OPENAI_KEY / [keys] openai set", model=model
        )
    if os.environ.get("OPENAI_API_KEY"):
        return BackendStatus(id=ID, available=True, detail="OPENAI_API_KEY set", model=model)
    return BackendStatus(id=ID, available=False, detail=NO_KEY_HINT, model=model)


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return response.text[:200]


def run(
    system: str,
    user: str,
    *,
    model: str,
    timeout_s: float = 90.0,
    client: httpx.Client | None = None,
) -> tuple[str, float | None, str | None]:
    """Run one completion; return ``(result_text, cost_usd, model_used)`` (cost is None)."""
    key = api_key()
    if not key:
        raise AssistError(f"OpenAI backend unavailable: {NO_KEY_HINT}")
    body: dict[str, Any] = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {key}"}
    own_client = client is None
    http = client or httpx.Client(timeout=timeout_s)
    try:
        response = http.post(API_URL, json=body, headers=headers, timeout=timeout_s)
    except httpx.TimeoutException:
        raise AssistError(f"OpenAI API timed out after {timeout_s:g}s") from None
    except httpx.HTTPError as exc:
        raise AssistError(f"could not reach the OpenAI API: {exc}") from None
    finally:
        if own_client:
            http.close()
    if response.status_code == 401:
        raise AssistError(
            "OpenAI API key invalid (HTTP 401): check CLIPFORAGE_OPENAI_KEY / [keys] openai"
        )
    if response.status_code >= 400:
        raise AssistError(f"OpenAI API error {response.status_code}: {_error_message(response)}")
    try:
        data = response.json()
        message = data["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise AssistError("OpenAI API returned an unexpected response") from None
    if message.get("refusal"):
        raise AssistError(f"OpenAI declined this request: {message['refusal']}")
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise AssistError("OpenAI API returned no text")
    return text, None, data.get("model") or model
