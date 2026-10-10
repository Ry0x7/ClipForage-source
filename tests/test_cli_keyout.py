"""``clipforage keyout`` and the service functions behind it. ffmpeg is never run."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from clipforage import service
from clipforage.cache import Cache
from clipforage.cli import app
from clipforage.download import service as dl
from clipforage.download.service import DownloadError
from clipforage.download.sidecar import write_sidecar
from clipforage.models import Asset, Kind

runner = CliRunner()


def _asset() -> Asset:
    return Asset(
        provider="internet_archive",
        id="cat_film",
        kind=Kind.VIDEO,
        title="Cat film",
        source_url="https://archive.org/details/cat_film",
        license="CC-BY-4.0",
        attribution_text="Cat film by Someone, CC BY 4.0",
        commercial_ok=True,
    )


def _fake_key_out(calls: list[dict[str, Any]]) -> Any:
    def fake(src: Path, **kwargs: Any) -> Path:
        calls.append({"src": src, **kwargs})
        progress = kwargs.get("progress")
        if progress:
            progress({"status": "downloading", "downloaded_bytes": None, "fraction": 0.5})
        out = src.with_name(src.stem + ".key.webm")
        out.write_bytes(b"webm")
        if progress:
            progress({"status": "finished", "filename": str(out), "fraction": 1.0})
        return out

    return fake


def _record(tmp_path: Path, name: str = "Cat film [cat_film] 1-3.mp4") -> tuple[int, Path]:
    path = tmp_path / name
    path.write_bytes(b"mp4")
    fetched = datetime(2026, 1, 2, tzinfo=UTC)
    extra = {"format": "mp4", "clip": {"start_s": 1.0, "end_s": 3.0}}
    sidecar = write_sidecar(path, _asset(), fetched, extra)
    with Cache() as cache:
        history_id = cache.record_download(
            url="https://archive.org/download/cat_film/cat_film.mp4",
            path=path,
            sidecar=sidecar,
            asset=_asset(),
            fetched_at=fetched,
            extra=extra,
        )
    return history_id, path


# ---------- CLI ----------


def test_keyout_history_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake(history_id: int, **kwargs: Any) -> dict[str, Any]:
        calls.append({"history_id": history_id, **kwargs})
        kwargs["progress"]({"status": "downloading", "downloaded_bytes": None, "fraction": 0.42})
        kwargs["progress"]({"status": "finished", "fraction": 1.0})
        return {"path": str(tmp_path / "a.key.webm"), "sidecar": str(tmp_path / "a.key.webm.json")}

    monkeypatch.setattr(service, "key_out_download", fake)
    result = runner.invoke(app, ["keyout", "12", "-t", "60", "--max-wall-s", "90"])

    assert result.exit_code == 0, result.output
    assert calls[0]["history_id"] == 12
    assert calls[0]["tolerance"] == 60
    assert calls[0]["max_wall_s"] == 90.0
    assert "keying 42%" in result.output
    assert f"saved    {tmp_path / 'a.key.webm'}" in result.output
    assert f"sidecar  {tmp_path / 'a.key.webm.json'}" in result.output


def test_keyout_default_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake(history_id: int, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"path": "x.key.webm", "sidecar": "x.key.webm.json"}

    monkeypatch.setattr(service, "key_out_download", fake)
    assert runner.invoke(app, ["keyout", "3"]).exit_code == 0
    assert calls[0]["tolerance"] == 80
    assert calls[0]["max_wall_s"] is None


def test_keyout_file_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dl, "key_out", _fake_key_out(calls))
    src = tmp_path / "green.mp4"
    src.write_bytes(b"mp4")

    result = runner.invoke(app, ["keyout", str(src)])

    assert result.exit_code == 0, result.output
    assert calls[0]["src"] == src
    assert calls[0]["tolerance"] == 80
    assert f"saved    {tmp_path / 'green.key.webm'}" in result.output
    assert "sidecar" not in result.output  # source has no sidecar
    assert not (tmp_path / "green.key.webm.json").exists()


def test_keyout_file_path_copies_sidecar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(dl, "key_out", _fake_key_out([]))
    src = tmp_path / "green.mp4"
    src.write_bytes(b"mp4")
    write_sidecar(src, _asset(), datetime.now(UTC), {"format": "mp4", "quality": "720"})

    result = runner.invoke(app, ["keyout", str(src), "--tolerance", "120"])

    assert result.exit_code == 0, result.output
    sidecar = tmp_path / "green.key.webm.json"
    assert f"sidecar  {sidecar}" in result.output
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    assert meta["license"] == "CC-BY-4.0"
    assert meta["attribution_text"] == "Cat film by Someone, CC BY 4.0"
    assert meta["format"] == "webm"
    assert meta["derived_from"] == "green.mp4"
    assert meta["quality"] == "720"
    assert meta["chroma"] == {"tolerance": 120, "similarity": 0.4, "blend": 0.1}


def test_keyout_errors_exit_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(history_id: int, **kwargs: Any) -> dict[str, Any]:
        raise DownloadError("ffmpeg not found; run `clipforage setup`")

    monkeypatch.setattr(service, "key_out_download", boom)
    result = runner.invoke(app, ["keyout", "5"])
    assert result.exit_code == 1
    assert "error: ffmpeg not found" in result.output
    assert "Traceback" not in result.output

    missing = runner.invoke(app, ["keyout", str(tmp_path / "nope.mp4")])
    assert missing.exit_code == 1
    assert "error: file no longer exists" in missing.output

    image = tmp_path / "pic.png"
    image.write_bytes(b"png")
    not_video = runner.invoke(app, ["keyout", str(image)])
    assert not_video.exit_code == 1
    assert "not a video file" in not_video.output


def test_keyout_refuses_keyed_export(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dl, "key_out", _fake_key_out(calls))
    history_id, path = _record(tmp_path, name="Cat film.key.webm")

    by_id = runner.invoke(app, ["keyout", str(history_id)])
    assert by_id.exit_code == 1
    assert "already a keyed export (.key.webm)" in by_id.output

    by_path = runner.invoke(app, ["keyout", str(path)])
    assert by_path.exit_code == 1
    assert "already a keyed export (.key.webm)" in by_path.output
    assert "Traceback" not in by_path.output
    assert calls == []

    upper = tmp_path / "Shout.KEY.WEBM"
    upper.write_bytes(b"webm")
    assert runner.invoke(app, ["keyout", str(upper)]).exit_code == 1


def test_keyout_unknown_history_id_exit_1() -> None:
    result = runner.invoke(app, ["keyout", "999"])
    assert result.exit_code == 1
    assert "error: no download with id 999" in result.output


def test_keyout_tolerance_out_of_range() -> None:
    result = runner.invoke(app, ["keyout", "1", "--tolerance", "200"])
    assert result.exit_code == 2


def test_history_prints_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    history_id, path = _record(tmp_path)
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"#{history_id}  2026-01-02T00:00:00+00:00  internet_archive")
    assert f"Cat film  ->  {path}" in result.output


# ---------- service ----------


def test_key_out_download_writes_sidecar_and_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dl, "key_out", _fake_key_out(calls))
    history_id, path = _record(tmp_path)

    meta = service.key_out_download(history_id, tolerance=40, max_wall_s=0)

    out = tmp_path / "Cat film [cat_film] 1-3.key.webm"
    assert calls[0]["src"] == path
    assert calls[0]["tolerance"] == 40
    assert calls[0]["max_wall_s"] == 0
    assert meta["path"] == str(out)
    assert meta["sidecar"] == str(out) + ".json"
    sidecar = json.loads(Path(meta["sidecar"]).read_text(encoding="utf-8"))
    assert sidecar["format"] == "webm"
    assert sidecar["derived_from"] == path.name
    assert sidecar["clip"] == {"start_s": 1.0, "end_s": 3.0}
    assert sidecar["license"] == "CC-BY-4.0"
    assert sidecar["chroma"]["similarity"] == round(0.05 + 0.35 * 40 / 120, 3)
    with Cache() as cache:
        row = cache.get_download(meta["history_id"])
    assert row is not None
    assert row["path"] == str(out)
    assert row["url"] == "https://archive.org/download/cat_film/cat_film.mp4"
    assert row["extra"]["derived_from"] == path.name


def test_key_out_download_uses_wall_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dl, "key_out", _fake_key_out(calls))
    monkeypatch.setenv("CLIPFORAGE_CLIP_MAX_WALL_S", "42")
    history_id, _ = _record(tmp_path)
    service.key_out_download(history_id)
    assert calls[0]["max_wall_s"] == 42.0


def test_key_out_download_rejects_non_video(tmp_path: Path) -> None:
    history_id, _ = _record(tmp_path, name="song.mp3")
    with pytest.raises(service.RefError, match="not a video file"):
        service.key_out_download(history_id)
