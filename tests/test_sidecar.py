import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from clipforage import __version__
from clipforage.download.sidecar import write_sidecar
from clipforage.models import Asset, Kind

EXPECTED_KEYS = {
    "license",
    "attribution_text",
    "source_url",
    "provider",
    "fetched_at",
    "asset_id",
    "title",
    "kind",
    "commercial_ok",
    "clipforage_version",
}


def _asset(title: str = "Surprised Pikachu") -> Asset:
    return Asset(
        provider="youtube",
        id="abc123",
        kind=Kind.VIDEO,
        title=title,
        source_url="https://www.youtube.com/watch?v=abc123",
        license="unknown",
        attribution_text=f'"{title}" by someone',
        commercial_ok=None,
    )


def test_sidecar_name_and_keys(tmp_path: Path) -> None:
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    fetched = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    path = write_sidecar(media, _asset(), fetched)

    assert path == tmp_path / "x.mp4.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == EXPECTED_KEYS
    assert data["fetched_at"] == "2026-10-01T12:00:00+00:00"
    assert data["provider"] == "youtube"
    assert data["asset_id"] == "abc123"
    assert data["kind"] == "video"
    assert data["commercial_ok"] is None
    assert data["clipforage_version"] == __version__


def test_sidecar_fetched_at_is_utc(tmp_path: Path) -> None:
    media = tmp_path / "x.mp4"
    plus_two = datetime(2026, 10, 1, 16, 0, tzinfo=timezone(timedelta(hours=2)))

    data = json.loads(write_sidecar(media, _asset(), plus_two).read_text(encoding="utf-8"))

    parsed = datetime.fromisoformat(data["fetched_at"])
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == datetime(2026, 10, 1, 14, 0, tzinfo=UTC)


def test_sidecar_extra_merged(tmp_path: Path) -> None:
    media = tmp_path / "x.mp4"
    extra = {"clip": {"start_s": 10, "end_s": 25}, "format": "mp4"}

    data = json.loads(
        write_sidecar(media, _asset(), datetime.now(UTC), extra).read_text(encoding="utf-8")
    )

    assert data["clip"] == {"start_s": 10, "end_s": 25}
    assert data["format"] == "mp4"
    assert EXPECTED_KEYS <= set(data)


def test_sidecar_non_ascii_preserved(tmp_path: Path) -> None:
    media = tmp_path / "x.mp4"
    title = "ピカチュウ café"

    path = write_sidecar(media, _asset(title), datetime.now(UTC))

    raw = path.read_text(encoding="utf-8")
    assert title in raw
    assert json.loads(raw)["title"] == title
