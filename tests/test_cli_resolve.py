"""`clipforage resolve-import`: summary output and exit codes (connect is patched)."""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from clipforage.cli import app
from clipforage.editors import resolve as rv

runner = CliRunner()


def _not_available() -> Any:
    raise rv.ResolveNotAvailable("could not connect to DaVinci Resolve (test)")


def test_resolve_import_not_connectable_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rv, "connect", _not_available)
    result = runner.invoke(app, ["resolve-import", "--library", str(tmp_path)])
    assert result.exit_code == 1
    assert "could not connect to DaVinci Resolve (test)" in result.output


def test_resolve_import_missing_library_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["resolve-import", "--library", str(tmp_path / "nope")])
    assert result.exit_code == 1
    assert "library folder not found" in result.output


def test_resolve_import_prints_summary_and_passes_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}
    sentinel = object()

    def fake_import(resolve: Any, library: Path, **kwargs: Any) -> rv.ImportReport:
        seen.update(resolve=resolve, library=library, **kwargs)
        return rv.ImportReport(
            bin_name=kwargs["bin_name"],
            imported=[str(tmp_path / "a.mp4")],
            skipped_existing=[str(tmp_path / "b.mp3")],
            failed=[str(tmp_path / "c.gif")],
        )

    monkeypatch.setattr(rv, "connect", lambda: sentinel)
    monkeypatch.setattr(rv, "import_library", fake_import)
    result = runner.invoke(
        app,
        ["resolve-import", "--library", str(tmp_path), "--bin", "memes", "--all", "--recursive"],
    )
    assert result.exit_code == 0, result.output
    assert seen == {
        "resolve": sentinel,
        "library": tmp_path,
        "bin_name": "memes",
        "only_new": False,
        "recursive": True,
    }
    assert "bin      memes" in result.output
    assert "imported: 1\n  a.mp4" in result.output
    assert "skipped (already in bin): 1\n  b.mp3" in result.output
    assert "failed: 1\n  c.gif" in result.output


def test_resolve_import_defaults_to_configured_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def fake_import(resolve: Any, library: Path, **kwargs: Any) -> rv.ImportReport:
        seen.update(library=library, **kwargs)
        return rv.ImportReport(bin_name=kwargs["bin_name"])

    monkeypatch.setattr("clipforage.config.library_dir", lambda: tmp_path)
    monkeypatch.setattr(rv, "connect", lambda: object())
    monkeypatch.setattr(rv, "import_library", fake_import)
    result = runner.invoke(app, ["resolve-import"])
    assert result.exit_code == 0, result.output
    assert seen == {
        "library": tmp_path,
        "bin_name": "clipforage",
        "only_new": True,
        "recursive": False,
    }
    assert "imported: 0" in result.output
