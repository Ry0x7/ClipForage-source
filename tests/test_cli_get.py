from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from clipforage import config, service
from clipforage.cli import app
from clipforage.download.service import DownloadError, asset_from_url
from clipforage.download.sidecar import write_sidecar
from clipforage.models import DownloadResult

runner = CliRunner()
URL = "https://www.youtube.com/watch?v=abc123"


def test_get_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_download(url: str, out_dir: Path | None = None, **kwargs: Any) -> DownloadResult:
        calls.append({"url": url, "out_dir": out_dir, **kwargs})
        progress = kwargs["progress"]
        progress({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        progress({"status": "downloading", "downloaded_bytes": 2_000_000})
        progress({"status": "finished"})
        media = tmp_path / "Big Buck Bunny [abc123].mp4"
        media.write_bytes(b"x")
        info = {"id": "abc123", "title": "Big Buck Bunny", "uploader": "Blender Foundation"}
        fetched = datetime.now(UTC)
        sidecar = write_sidecar(media, asset_from_url(url, info), fetched)
        return DownloadResult(path=media, sidecar=sidecar, fetched_at=fetched)

    monkeypatch.setattr(service, "download_url", fake_download)

    result = runner.invoke(
        app, ["get", URL, "--mp3", "--clip", "00:05-00:12", "--out", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "saved" in result.output
    assert "sidecar" in result.output
    assert "license  unknown" in result.output
    assert "Blender Foundation" in result.output
    assert "50.0%" in result.output
    assert calls[0]["audio_only"] is True
    assert calls[0]["clip"] == (5.0, 12.0)
    assert calls[0]["out_dir"] == tmp_path


def test_get_forwards_cookies_from_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_download(url: str, out_dir: Path | None = None, **kwargs: Any) -> DownloadResult:
        calls.append(kwargs)
        raise DownloadError("stop here")

    monkeypatch.setattr(service, "download_url", fake_download)

    result = runner.invoke(app, ["get", URL, "--cookies-from-browser", "firefox"])

    assert result.exit_code == 1
    assert calls[0]["cookies_from_browser"] == "firefox"


def test_get_ref_forwards_max_wall_s(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_download_ref(ref: str, **kwargs: Any) -> Any:
        calls.append({"ref": ref, **kwargs})
        raise DownloadError("stop here")

    monkeypatch.setattr(service, "resolve_ref", lambda ref: (None, None))
    monkeypatch.setattr(service, "download_ref", fake_download_ref)

    result = runner.invoke(
        app, ["get", "internet_archive:cat_film", "--clip", "1-3", "--max-wall-s", "20"]
    )

    assert result.exit_code == 1
    assert "stop here" in result.output
    assert calls[0]["ref"] == "internet_archive:cat_film"
    assert calls[0]["clip"] == (1.0, 3.0)
    assert calls[0]["max_wall_s"] == 20.0


def test_get_ref_max_wall_s_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_download_ref(ref: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        raise DownloadError("stop here")

    monkeypatch.setattr(service, "resolve_ref", lambda ref: (None, None))
    monkeypatch.setattr(service, "download_ref", fake_download_ref)

    runner.invoke(app, ["get", "internet_archive:cat_film", "--clip", "1-3"])

    assert calls[0]["max_wall_s"] is None  # service falls back to config / 300 s


def test_get_defaults_to_configured_library_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path | None] = []

    def fake_download(url: str, out_dir: Path | None = None, **kwargs: Any) -> DownloadResult:
        calls.append(out_dir)
        raise DownloadError("stop here")

    monkeypatch.setattr(service, "download_url", fake_download)
    library = tmp_path / "configured-library"
    config.set_setting("library", "dir", str(library))

    result = runner.invoke(app, ["get", URL])

    assert result.exit_code == 1
    assert calls == [library]


def test_get_bad_clip(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*args: Any, **kwargs: Any) -> DownloadResult:
        raise AssertionError("download_url must not be called")

    monkeypatch.setattr(service, "download_url", never)

    result = runner.invoke(app, ["get", URL, "--clip", "25-10"])

    assert result.exit_code == 1
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_get_download_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> DownloadResult:
        raise DownloadError("Sign in to confirm you're not a bot\nhint: try again later")

    monkeypatch.setattr(service, "download_url", boom)

    result = runner.invoke(app, ["get", URL])

    assert result.exit_code == 1
    assert "error: Sign in" in result.output
    assert "Traceback" not in result.output


def test_history_empty() -> None:
    result = runner.invoke(app, ["history"])

    assert result.exit_code == 0, result.output
    assert "no downloads yet" in result.output


def test_get_records_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_download(url: str, out_dir: Path | None = None, **kwargs: Any) -> DownloadResult:
        media = tmp_path / "Big Buck Bunny [abc123].mp4"
        media.write_bytes(b"x")
        info = {"id": "abc123", "title": "Big Buck Bunny", "uploader": "Blender Foundation"}
        fetched = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        sidecar = write_sidecar(media, asset_from_url(url, info), fetched, {"format": "mp4"})
        return DownloadResult(path=media, sidecar=sidecar, fetched_at=fetched)

    monkeypatch.setattr(service, "download_url", fake_download)

    got = runner.invoke(app, ["get", URL, "--out", str(tmp_path)])
    assert got.exit_code == 0, got.output
    assert "warning" not in got.output

    result = runner.invoke(app, ["history"])

    assert result.exit_code == 0, result.output
    media = tmp_path / "Big Buck Bunny [abc123].mp4"
    assert f"#1  2026-01-02T03:04:05+00:00  youtube  Big Buck Bunny  ->  {media}" in result.output


def test_get_history_failure_is_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_download(url: str, out_dir: Path | None = None, **kwargs: Any) -> DownloadResult:
        media = tmp_path / "clip.mp4"
        media.write_bytes(b"x")
        fetched = datetime.now(UTC)
        sidecar = write_sidecar(media, asset_from_url(url, {"id": "abc123"}), fetched)
        return DownloadResult(path=media, sidecar=sidecar, fetched_at=fetched)

    def broken_cache(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(service, "download_url", fake_download)
    monkeypatch.setattr(service, "Cache", broken_cache)

    result = runner.invoke(app, ["get", URL])

    assert result.exit_code == 0, result.output
    assert "warning: could not record download history: read-only file system" in result.output


def test_get_forwards_quality(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_download(url: str, out_dir: Path | None = None, **kwargs: Any) -> DownloadResult:
        calls.append(kwargs)
        raise DownloadError("stop here")

    monkeypatch.setattr(service, "download_url", fake_download)

    result = runner.invoke(app, ["get", URL, "--quality", "720"])

    assert result.exit_code == 1
    assert "stop here" in result.output
    assert calls[0]["quality"] == "720"


def test_get_ref_forwards_quality(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_download_ref(ref: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        raise DownloadError("stop here")

    monkeypatch.setattr(service, "resolve_ref", lambda ref: (None, None))
    monkeypatch.setattr(service, "download_ref", fake_download_ref)

    runner.invoke(app, ["get", "youtube:abc123", "-q", "480"])

    assert calls[0]["quality"] == "480"


def test_get_bad_quality(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*args: Any, **kwargs: Any) -> DownloadResult:
        raise AssertionError("download_url must not be called")

    monkeypatch.setattr(service, "download_url", never)

    result = runner.invoke(app, ["get", URL, "--quality", "999"])

    assert result.exit_code == 1
    assert "error: unknown quality '999'" in result.output
    assert "best, 2160, 1440, 1080, 720, 480, 360" in result.output
    assert "Traceback" not in result.output


def test_get_ref_mp3_on_direct_mp3_plan_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforage.models import Asset, DownloadPlan, Kind
    from clipforage.providers import registry

    asset = Asset(
        provider="fakemp3",
        id="7",
        kind=Kind.AUDIO_SFX,
        title="Whoosh",
        source_url="https://example.org/whoosh",
        license="CC0-1.0",
        attribution_text="Whoosh by someone (CC0)",
        commercial_ok=True,
    )

    class FakeMp3Provider:
        id = "fakemp3"

        def resolve(self, asset: Asset) -> DownloadPlan:
            return DownloadPlan(
                asset=asset, method="http", url="https://cdn.example.org/w.mp3", ext="mp3"
            )

    calls: list[DownloadPlan] = []

    def fake_download_plan(plan: DownloadPlan, out_dir: Path | None = None, **kw: Any) -> Any:
        calls.append(plan)
        media = tmp_path / "Whoosh [7].mp3"
        media.write_bytes(b"x")
        fetched = datetime.now(UTC)
        sidecar = write_sidecar(media, plan.asset, fetched)
        return DownloadResult(path=media, sidecar=sidecar, fetched_at=fetched)

    monkeypatch.setattr(registry, "load_builtins", lambda: None)
    monkeypatch.setattr(service, "resolve_ref", lambda ref: (FakeMp3Provider(), asset))
    monkeypatch.setattr(service, "download_plan", fake_download_plan)

    result = runner.invoke(app, ["get", "fakemp3:7", "--mp3", "--out", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "saved" in result.output
    assert len(calls) == 1
    assert calls[0].ext == "mp3"
