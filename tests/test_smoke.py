from typer.testing import CliRunner

import clipforage
from clipforage.cli import app

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
