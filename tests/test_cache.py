import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clipforage import cache as cache_mod
from clipforage import config
from clipforage.cache import DEFAULT_TTL_S, Cache, make_key
from clipforage.models import Asset, Kind, SearchFilters

FILTERS = SearchFilters(kind=Kind.VIDEO)


def make_asset(asset_id: str, **overrides: object) -> Asset:
    data: dict[str, object] = {
        "provider": "fake",
        "id": asset_id,
        "kind": Kind.VIDEO,
        "title": f"asset {asset_id}",
        "source_url": f"https://example.com/{asset_id}",
        "license": "CC0-1.0",
        "attribution_text": "someone",
        "commercial_ok": True,
    }
    data.update(overrides)
    return Asset(**data)  # type: ignore[arg-type]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Cache]:
    with Cache(tmp_path / "sub" / "cache.db") as c:
        yield c


def test_default_ttl() -> None:
    assert DEFAULT_TTL_S == 3600


def test_creates_parent_dir_and_schema_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "cache.db"
    with Cache(path):
        pass
    assert path.is_file()
    with Cache(path) as again:
        assert again.get_search("fake", "q", FILTERS) is None


def test_db_path_uses_data_dir() -> None:
    assert cache_mod.db_path() == config.data_dir() / "cache.db"


def test_miss_returns_none(db: Cache) -> None:
    assert db.get_search("fake", "cats", FILTERS, now=1000.0) is None


def test_put_then_get_round_trips(db: Cache) -> None:
    assets = [
        make_asset("1", duration_s=12.5, orientation="portrait", extra={"w": 1080}),
        make_asset("2", commercial_ok=None, license="unknown"),
    ]
    db.put_search("fake", "cats", FILTERS, assets, ttl_s=60, now=1000.0)
    got = db.get_search("fake", "cats", FILTERS, now=1030.0)
    assert got == assets
    assert all(isinstance(a, Asset) for a in got or [])


def test_expired_entry_is_miss_and_deleted(db: Cache) -> None:
    db.put_search("fake", "cats", FILTERS, [make_asset("1")], ttl_s=60, now=1000.0)
    assert db.get_search("fake", "cats", FILTERS, now=1060.0) is not None  # exactly ttl: hit
    assert db.get_search("fake", "cats", FILTERS, now=1061.0) is None
    # the row is gone, so even an earlier "now" misses
    assert db.get_search("fake", "cats", FILTERS, now=1000.0) is None


def test_key_differs_by_filters_provider_and_query() -> None:
    base = make_key("fake", "cats", FILTERS)
    assert base == make_key("fake", "cats", SearchFilters(kind=Kind.VIDEO))
    assert base != make_key("fake", "cats", SearchFilters(kind=Kind.VIDEO, commercial_only=True))
    assert base != make_key("fake", "cats", SearchFilters(kind=Kind.IMAGE))
    assert base != make_key("other", "cats", FILTERS)
    assert base != make_key("fake", "dogs", FILTERS)


def test_get_respects_filters(db: Cache) -> None:
    db.put_search("fake", "cats", FILTERS, [make_asset("1")], ttl_s=60, now=1000.0)
    other = SearchFilters(kind=Kind.VIDEO, orientation="portrait")
    assert db.get_search("fake", "cats", other, now=1000.0) is None


def test_style_and_limit_change_cache_key(db: Cache) -> None:
    assert make_key("fake", "cats", FILTERS) != make_key(
        "fake", "cats", SearchFilters(kind=Kind.VIDEO, style="meme")
    )
    assert make_key("fake", "cats", FILTERS) != make_key(
        "fake", "cats", SearchFilters(kind=Kind.VIDEO, limit=8)
    )
    meme = SearchFilters(kind=Kind.VIDEO, style="meme")
    db.put_search("fake", "cats", meme, [make_asset("1")], ttl_s=60, now=1000.0)
    hit = db.get_search("fake", "cats", meme, now=1000.0)
    assert hit is not None and [a.id for a in hit] == ["1"]
    assert db.get_search("fake", "cats", FILTERS, now=1000.0) is None
    cinematic = SearchFilters(kind=Kind.VIDEO, style="cinematic")
    assert db.get_search("fake", "cats", cinematic, now=1000.0) is None


def test_clear_search_counts_rows(db: Cache) -> None:
    assert db.clear_search() == 0
    db.put_search("fake", "a", FILTERS, [make_asset("1")], ttl_s=60)
    db.put_search("fake", "b", FILTERS, [], ttl_s=60)
    assert db.clear_search() == 2
    assert db.get_search("fake", "a", FILTERS) is None


def test_record_download_and_history_newest_first(db: Cache, tmp_path: Path) -> None:
    assert db.history() == []
    for i in range(3):
        db.record_download(
            url=f"https://example.com/{i}",
            path=tmp_path / f"{i}.mp4",
            sidecar=tmp_path / f"{i}.mp4.json",
            asset=make_asset(str(i)),
            fetched_at=datetime(2026, 1, 1, 12, i, tzinfo=UTC),
            extra={"format": "mp4"} if i == 2 else None,
        )
    rows = db.history()
    assert [r["asset_id"] for r in rows] == ["2", "1", "0"]
    assert rows[0]["path"] == str(tmp_path / "2.mp4")
    assert rows[0]["title"] == "asset 2"
    assert rows[0]["license"] == "CC0-1.0"
    assert rows[0]["provider"] == "fake"
    assert rows[0]["fetched_at"] == "2026-01-01T12:02:00+00:00"
    assert rows[0]["extra"] == {"format": "mp4"}
    assert rows[1]["extra"] == {}
    assert [r["asset_id"] for r in db.history(limit=2)] == ["2", "1"]


def test_record_download_returns_id_and_get_download(db: Cache, tmp_path: Path) -> None:
    ids = [
        db.record_download(
            url=f"https://example.com/{i}",
            path=tmp_path / f"{i}.mp4",
            sidecar=tmp_path / f"{i}.mp4.json",
            asset=make_asset(str(i)),
            fetched_at=datetime(2026, 1, 1, 12, i, tzinfo=UTC),
            extra={"format": "mp4"},
        )
        for i in range(2)
    ]
    assert ids[1] == ids[0] + 1
    row = db.get_download(ids[1])
    assert row == db.history(limit=1)[0]
    assert row is not None and row["id"] == ids[1] and row["extra"] == {"format": "mp4"}
    assert db.get_download(ids[1] + 100) is None


def test_latest_download_for_path_picks_newest(db: Cache, tmp_path: Path) -> None:
    target = tmp_path / "same.mp4"
    first = db.record_download(
        url="https://example.com/a",
        path=target,
        sidecar=tmp_path / "same.mp4.json",
        asset=make_asset("a"),
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = db.record_download(
        url="https://example.com/a",
        path=target,
        sidecar=tmp_path / "same.mp4.json",
        asset=make_asset("a"),
        fetched_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert second > first
    row = db.latest_download_for_path(target)
    assert row is not None and row["id"] == second
    assert db.latest_download_for_path(tmp_path / "missing.mp4") is None


def test_latest_download_for_asset_picks_newest(db: Cache, tmp_path: Path) -> None:
    ids = [
        db.record_download(
            url="https://example.com/a",
            path=tmp_path / name,
            sidecar=tmp_path / f"{name}.json",
            asset=make_asset(asset_id),
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for name, asset_id in (("a1.mp4", "a"), ("b.mp4", "b"), ("a2.mp4", "a"))
    ]
    row = db.latest_download_for_asset("fake", "a")
    assert row is not None and row["id"] == ids[2] and row["path"] == str(tmp_path / "a2.mp4")
    assert db.latest_download_for_asset("fake", "missing") is None
    assert db.latest_download_for_asset("other", "a") is None
    indexes = {r[1] for r in db._conn.execute("PRAGMA index_list(download_history)")}
    assert "download_history_asset" in indexes
    Cache(db.path).close()  # schema setup (incl. the index) is idempotent on reopen


@pytest.mark.skipif(os.name != "nt", reason="LOCALAPPDATA branch is Windows-only")
def test_data_dir_windows_uses_localappdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert config.data_dir() == tmp_path / "local" / "clipforage"


@pytest.mark.skipif(os.name != "nt", reason="APPDATA fallback is Windows-only")
def test_data_dir_windows_falls_back_to_appdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert config.data_dir() == tmp_path / "roaming" / "clipforage"


@pytest.mark.skipif(os.name == "nt", reason="XDG branch is non-Windows only")
def test_data_dir_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert config.data_dir() == tmp_path / "xdg" / "clipforage"


def test_find_asset_scans_cached_results(db: Cache) -> None:
    db.put_search("fake", "cats", FILTERS, [make_asset("a"), make_asset("b")], ttl_s=60, now=1000.0)
    db.put_search(
        "other", "cats", FILTERS, [make_asset("c", provider="other")], ttl_s=60, now=1000.0
    )
    found = db.find_asset("fake", "b", now=1010.0)
    assert found is not None and found.id == "b"
    assert db.find_asset("fake", "c", now=1010.0) is None
    assert db.find_asset("fake", "missing", now=1010.0) is None
    assert db.find_asset("fake", "a", now=2000.0) is None  # expired entries are ignored
