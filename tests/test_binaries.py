import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from clipforage import cli
from clipforage.cli import app
from clipforage.download import binaries
from clipforage.download.binaries import (
    SetupError,
    bin_dir,
    exe,
    extract_members,
    find_tool,
    install,
    parse_sha256,
    releases_for,
)
from clipforage.download.service import build_ydl_opts

runner = CliRunner()

HASH = "ab" * 32


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --- releases_for -----------------------------------------------------------


def test_releases_windows_amd64() -> None:
    rel = releases_for("Windows", "AMD64")
    assert rel["deno"].url == (
        "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
    )
    assert rel["deno"].checksum_url == rel["deno"].url + ".sha256sum"
    assert rel["deno"].members == ("deno.exe",)
    assert rel["ffmpeg"].url == (
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-win64-gpl.zip"
    )
    assert rel["ffmpeg"].checksum_url == (
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256"
    )
    assert rel["ffmpeg"].members == ("bin/ffmpeg.exe", "bin/ffprobe.exe")


def test_releases_windows_arm64() -> None:
    rel = releases_for("Windows", "ARM64")
    assert rel["deno"].url.endswith("deno-aarch64-pc-windows-msvc.zip")
    assert rel["ffmpeg"].url.endswith("ffmpeg-master-latest-winarm64-gpl.zip")


def test_releases_darwin_has_deno_only() -> None:
    rel = releases_for("Darwin", "arm64")
    assert set(rel) == {"deno"}
    assert rel["deno"].url.endswith("deno-aarch64-apple-darwin.zip")
    assert rel["deno"].members == ("deno",)


def test_releases_linux() -> None:
    rel = releases_for("Linux", "x86_64")
    assert rel["ffmpeg"].url.endswith("linux64-gpl.tar.xz")
    assert rel["ffmpeg"].members == ("bin/ffmpeg", "bin/ffprobe")
    assert rel["deno"].url.endswith("deno-x86_64-unknown-linux-gnu.zip")
    assert releases_for("Linux", "aarch64")["ffmpeg"].url.endswith("linuxarm64-gpl.tar.xz")


# --- parse_sha256 -----------------------------------------------------------


def test_parse_sha256_powershell_format() -> None:
    text = (
        "\r\nAlgorithm       Hash                                                   Path\r\n"
        "---------       ----                                                   ----\r\n"
        f"SHA256          {HASH.upper()}       "
        "D:\\a\\deno\\deno\\target\\release\\deno-x86_64-pc-windows-msvc.zip\r\n\r\n"
    )
    assert parse_sha256(text, "deno-x86_64-pc-windows-msvc.zip") == HASH


def test_parse_sha256_aggregate_picks_matching_line() -> None:
    other = "cd" * 32
    text = (
        f"{other}  ffmpeg-master-latest-win64-gpl-shared.zip\n"
        f"{HASH}  ffmpeg-master-latest-win64-gpl.zip\n"
        f"{'ef' * 32}  ffmpeg-master-latest-linux64-gpl.tar.xz\n"
    )
    assert parse_sha256(text, "ffmpeg-master-latest-win64-gpl.zip") == HASH
    assert parse_sha256(text, "ffmpeg-master-latest-win64-gpl-shared.zip") == other
    assert parse_sha256(text, "not-listed.zip") is None


def test_parse_sha256_garbage() -> None:
    assert parse_sha256("<html>Not Found</html>", "x.zip") is None
    assert parse_sha256("", None) is None


# --- find_tool --------------------------------------------------------------


def test_find_tool_prefers_bin_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda name: "C:/elsewhere/deno.exe")
    assert find_tool("deno") == Path("C:/elsewhere/deno.exe")
    bin_dir().mkdir(parents=True)
    local = bin_dir() / exe("deno")
    local.write_bytes(b"fake")
    assert find_tool("deno") == local


def test_find_tool_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    assert find_tool("ffmpeg") is None


# --- extract_members --------------------------------------------------------


def test_extract_members_zip(tmp_path: Path) -> None:
    archive = tmp_path / "a.zip"
    archive.write_bytes(
        _zip_bytes(
            {
                "ffmpeg-xxx/bin/ffmpeg.exe": b"ffmpeg",
                "ffmpeg-xxx/bin/ffprobe.exe": b"ffprobe",
                "ffmpeg-xxx/doc/readme.txt": b"doc",
                "deno.exe": b"deno",
            }
        )
    )
    out = tmp_path / "out"

    written = extract_members(archive, ("bin/ffmpeg.exe", "bin/ffprobe.exe", "deno.exe"), out)

    assert written == [out / "ffmpeg.exe", out / "ffprobe.exe", out / "deno.exe"]
    assert (out / "ffmpeg.exe").read_bytes() == b"ffmpeg"
    assert (out / "deno.exe").read_bytes() == b"deno"
    assert sorted(p.name for p in out.iterdir()) == ["deno.exe", "ffmpeg.exe", "ffprobe.exe"]


def test_extract_members_missing(tmp_path: Path) -> None:
    archive = tmp_path / "a.zip"
    archive.write_bytes(_zip_bytes({"deno.exe": b"deno"}))
    with pytest.raises(SetupError, match="bin/ffmpeg.exe"):
        extract_members(archive, ("bin/ffmpeg.exe",), tmp_path / "out")


# --- install (respx) --------------------------------------------------------


def _deno_release() -> binaries.Release:
    return binaries.Release(
        name="deno-test.zip",
        url="https://example.test/deno-test.zip",
        checksum_url="https://example.test/deno-test.zip.sha256sum",
        members=(exe("deno"),),
    )


@respx.mock
def test_install_deno_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    release = _deno_release()
    monkeypatch.setattr(binaries, "releases_for", lambda: {"deno": release})
    payload = _zip_bytes({exe("deno"): b"deno binary"})
    digest = hashlib.sha256(payload).hexdigest()
    respx.get(release.url).mock(return_value=httpx.Response(200, content=payload))
    respx.get(release.checksum_url).mock(
        return_value=httpx.Response(
            200,
            text=(
                "\r\nAlgorithm       Hash          Path\r\n"
                "---------       ----          ----\r\n"
                f"SHA256          {digest.upper()}  D:\\a\\target\\release\\deno-test.zip\r\n"
            ),
        )
    )
    messages: list[str] = []
    seen: list[tuple[int, int | None]] = []

    path = install("deno", echo=messages.append, progress=lambda d, t: seen.append((d, t)))

    assert path == bin_dir() / exe("deno")
    assert path.read_bytes() == b"deno binary"
    assert not (bin_dir() / release.name).exists()
    assert not (bin_dir() / (release.name + ".part")).exists()
    assert seen and seen[-1] == (len(payload), len(payload))

    again = install("deno", echo=messages.append)
    assert again == path
    assert any("already installed" in m for m in messages)


@respx.mock
def test_install_checksum_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    release = _deno_release()
    monkeypatch.setattr(binaries, "releases_for", lambda: {"deno": release})
    respx.get(release.url).mock(
        return_value=httpx.Response(200, content=_zip_bytes({exe("deno"): b"x"}))
    )
    respx.get(release.checksum_url).mock(
        return_value=httpx.Response(200, text=f"{HASH}  deno-test.zip\n")
    )

    with pytest.raises(SetupError, match="checksum mismatch for deno-test.zip"):
        install("deno", echo=lambda m: None)

    assert not (bin_dir() / exe("deno")).exists()
    assert not (bin_dir() / release.name).exists()


@respx.mock
def test_install_checksum_unavailable_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    release = _deno_release()
    monkeypatch.setattr(binaries, "releases_for", lambda: {"deno": release})
    respx.get(release.url).mock(
        return_value=httpx.Response(200, content=_zip_bytes({exe("deno"): b"x"}))
    )
    respx.get(release.checksum_url).mock(return_value=httpx.Response(404))

    with pytest.raises(SetupError, match="checksum"):
        install("deno", echo=lambda m: None)

    assert not (bin_dir() / exe("deno")).exists()


@respx.mock
def test_install_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    release = _deno_release()
    monkeypatch.setattr(binaries, "releases_for", lambda: {"deno": release})
    respx.get(release.url).mock(return_value=httpx.Response(500))

    with pytest.raises(SetupError, match="HTTP 500"):
        install("deno", echo=lambda m: None)


# --- build_ydl_opts deno ----------------------------------------------------


def test_opts_deno(tmp_path: Path) -> None:
    opts = build_ydl_opts(
        tmp_path, audio_only=False, clip=None, ffmpeg=None, progress=None, deno="C:/x/deno.exe"
    )
    assert opts["js_runtimes"] == {"deno": {"path": "C:/x/deno.exe"}}
    default = build_ydl_opts(tmp_path, audio_only=False, clip=None, ffmpeg=None, progress=None)
    assert "js_runtimes" not in default


# --- CLI --------------------------------------------------------------------


def test_doctor_reports_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda name: None)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    for tool in ("ffmpeg", "deno", "yt-dlp", "yt-dlp-ejs"):
        assert tool in result.stdout
    assert "clipforage setup" in result.stdout
    assert binaries.ytdlp_version() in result.stdout


def test_setup_tool_deno(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_install(tool: str, **kwargs: object) -> Path:
        calls.append(tool)
        return Path("C:/data/bin/deno.exe")

    monkeypatch.setattr(cli.binaries, "install", fake_install)
    result = runner.invoke(app, ["setup", "--tool", "deno"])
    assert result.exit_code == 0
    assert calls == ["deno"]
    assert f"installed deno -> {Path('C:/data/bin/deno.exe')}" in result.stdout


def test_setup_error_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_install(tool: str, **kwargs: object) -> Path:
        raise SetupError(f"checksum mismatch for {tool}.zip")

    monkeypatch.setattr(cli.binaries, "install", fake_install)
    result = runner.invoke(app, ["setup", "--tool", "deno"])
    assert result.exit_code == 1
    assert "error: checksum mismatch" in result.stderr


def test_setup_continues_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_install(tool: str, **kwargs: object) -> Path:
        calls.append(tool)
        if tool == "ffmpeg":
            raise SetupError("boom")
        return Path("C:/data/bin/deno.exe")

    monkeypatch.setattr(cli.binaries, "install", fake_install)
    monkeypatch.setattr(cli.binaries, "releases_for", lambda: releases_for("Windows", "AMD64"))
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 1
    assert calls == ["ffmpeg", "deno"]
    assert "installed deno" in result.stdout


def test_setup_invalid_tool() -> None:
    result = runner.invoke(app, ["setup", "--tool", "vlc"])
    assert result.exit_code == 1
    assert "invalid tool" in result.stderr
