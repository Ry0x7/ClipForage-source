import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from clipforage.providers import registry


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    registry.clear()
    yield
    registry.clear()


@pytest.fixture(autouse=True)
def _isolated_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every user-dir env var at tmp_path so no test touches real user dirs."""
    for var, sub in (
        ("APPDATA", "appdata"),
        ("LOCALAPPDATA", "localappdata"),
        ("XDG_DATA_HOME", "xdg_data"),
        ("XDG_CONFIG_HOME", "xdg_config"),
        ("HOME", "home"),
        ("USERPROFILE", "home"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))


@pytest.fixture(autouse=True)
def _no_legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop pre-rename MEMEFINDER_* vars so the legacy env fallback never leaks into tests."""
    for var in list(os.environ):
        if var.upper().startswith("MEMEFINDER_"):
            monkeypatch.delenv(var)


@pytest.fixture(autouse=True)
def _no_safe_search_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user's CLIPFORAGE_SAFE_SEARCH must not leak in: tests see the default (on)."""
    monkeypatch.delenv("CLIPFORAGE_SAFE_SEARCH", raising=False)


@pytest.fixture(autouse=True)
def _scrape_off_by_default(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Scraping providers / KYM lookup stay offline unless a test turns them on explicitly."""
    from clipforage import scrape

    monkeypatch.setenv("CLIPFORAGE_SCRAPE_ENABLED", "0")
    scrape.reset()
    yield
    scrape.reset()
