"""Shared ``httpx`` client that sends the project User-Agent."""

import threading
from typing import Any

import httpx

from clipforage import __version__

USER_AGENT: str = f"clipforage/{__version__} (+https://github.com/Ry0x7/ClipForage-source)"

_client: httpx.Client | None = None
_lock = threading.Lock()


def make_client(timeout: float = 20.0, **kwargs: Any) -> httpx.Client:
    """Build an ``httpx.Client`` with the project User-Agent, redirects on and ``timeout``.

    Extra ``headers`` are merged over the User-Agent; other kwargs go to ``httpx.Client``.
    """
    headers = {"User-Agent": USER_AGENT, **dict(kwargs.pop("headers", None) or {})}
    kwargs.setdefault("follow_redirects", True)
    return httpx.Client(headers=headers, timeout=timeout, **kwargs)


def get_client() -> httpx.Client:
    """Return the process-wide shared client, creating it on first use."""
    global _client
    with _lock:
        if _client is None or _client.is_closed:
            _client = make_client()
        return _client


def reset_client() -> None:
    """Close and drop the shared client (used by tests)."""
    global _client
    with _lock:
        if _client is not None:
            _client.close()
        _client = None
