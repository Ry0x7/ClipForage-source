import sys

import pytest
from typer.testing import CliRunner

import clipforage
from clipforage import config
from clipforage.cli import app, legacy_main

runner = CliRunner()


def test_import() -> None:
    assert clipforage.__version__ == "0.1.0"


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_doctor_exits_zero() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    for tool in ("ffmpeg", "deno", "yt-dlp"):
        assert tool in result.stdout


def test_legacy_main_warns_then_runs_app(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["memefinder", "version"])
    with pytest.raises(SystemExit) as exc:
        legacy_main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.err.splitlines()[0] == (
        "memefinder was renamed to clipforage; this alias will be removed"
    )
    assert captured.out.strip() == "0.1.0"


def test_cli_migrates_legacy_dirs_and_reports_on_stderr() -> None:
    old = config._config_dir("memefinder")
    old.mkdir(parents=True)
    (old / "config.toml").write_text('[keys]\npixabay = "x"\n', encoding="utf-8")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "renamed to clipforage" in result.stderr
    assert "renamed" not in result.stdout
    assert config.get_key("pixabay") == "x"
    assert "renamed" not in runner.invoke(app, ["version"]).stderr
