"""Typer-free service layer: refs, search, download, history, providers. No live network."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforage import rights, service
from clipforage.cache import Cache
from clipforage.download.sidecar import write_sidecar
from clipforage.models import (
    Asset,
    DownloadPlan,
    DownloadResult,
    Kind,
    SearchFilters,
    Style,
)
from clipforage.providers import registry
from clipforage.providers.base import ProviderError
from clipforage.providers.openverse import AUDIO_URL as OPENVERSE_AUDIO_URL
from clipforage.providers.openverse import OpenverseProvider

FILTERS = SearchFilters(kind=Kind.VIDEO)
MEDIA_URL = "https://cdn.example.com/rain.mp4"


def make_asset(asset_id: str = "42") -> Asset:
    return Asset(
        provider="fakehttp",
        id=asset_id,
        kind=Kind.VIDEO,
        title="Rain on window",
        source_url=f"https://example.com/{asset_id}",
        license="CC0-1.0",
        attribution_text="Rain by Someone (CC0)",
        commercial_ok=True,
        extra={"download_url": MEDIA_URL},
    )


class FakeProvider:
    kinds = frozenset({Kind.VIDEO})

    def __init__(
        self, provider_id: str, assets: list[Asset] | None = None, requires_key: bool = False
    ) -> None:
        self.id = provider_id
        self.assets = assets or []
        self.requires_key = requires_key

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        return list(self.assets)

    def resolve(self, asset: Asset) -> DownloadPlan:
        return DownloadPlan(asset=asset, method="http", url=asset.extra["download_url"], ext="mp4")


@pytest.fixture(autouse=True)
def _no_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "load_builtins", lambda: None)


def cache_asset(asset: Asset) -> None:
    with Cache() as cache:
        cache.put_search(asset.provider, "rain", FILTERS, [asset], ttl_s=3600)


def test_parse_ref_good() -> None:
    assert service.parse_ref("pexels:3571264") == ("pexels", "3571264")
    assert service.parse_ref("internet_archive:a:b") == ("internet_archive", "a:b")


@pytest.mark.parametrize("ref", ["", "pexels", "pexels:", "Pexels:1", "pex-els:1", ":1"])
def test_parse_ref_bad(ref: str) -> None:
    with pytest.raises(service.RefError) as info:
        service.parse_ref(ref)
    assert "is not a URL" in str(info.value)
    assert info.value.hint


def test_resolve_ref_unknown_provider() -> None:
    registry.register(FakeProvider("fakehttp"))
    with pytest.raises(service.RefError) as info:
        service.resolve_ref("nope:1")
    assert "providers: fakehttp" in str(info.value)


def test_resolve_ref_not_cached_has_hint() -> None:
    registry.register(FakeProvider("fakehttp"))
    with pytest.raises(service.RefError) as info:
        service.resolve_ref("fakehttp:missing")
    assert info.value.hint == "run a search first so the asset is cached"
    assert "not in the search cache" in str(info.value)


def test_resolve_ref_cached() -> None:
    provider = registry.register(FakeProvider("fakehttp"))
    cache_asset(make_asset())
    got_provider, asset = service.resolve_ref("fakehttp:42")
    assert got_provider is provider
    assert asset.title == "Rain on window"


@respx.mock
def test_download_ref_http_plan(tmp_path: Path) -> None:
    registry.register(FakeProvider("fakehttp"))
    cache_asset(make_asset())
    media = respx.get(MEDIA_URL).mock(return_value=httpx.Response(200, content=b"mp4 bytes"))

    url, result = service.download_ref("fakehttp:42", out=tmp_path)

    assert url == MEDIA_URL
    assert media.call_count == 1
    assert result.path == tmp_path / "Rain on window [42].mp4"
    assert result.path.read_bytes() == b"mp4 bytes"
    meta = json.loads(result.sidecar.read_text(encoding="utf-8"))
    assert meta["license"] == "CC0-1.0"
    assert meta["provider"] == "fakehttp"


@pytest.mark.parametrize("kwargs", [{"mp3": True}, {"mp3": True, "clip": (1.0, 2.0)}])
@respx.mock
def test_download_ref_http_plan_rejects_mp3(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    registry.register(FakeProvider("fakehttp"))
    cache_asset(make_asset())
    media = respx.get(MEDIA_URL).mock(return_value=httpx.Response(200, content=b"x"))

    with pytest.raises(service.RefError) as info:
        service.download_ref("fakehttp:42", out=tmp_path / "library", **kwargs)

    assert "--mp3 only applies to yt-dlp sources" in str(info.value)
    assert info.value.hint == "download without mp3; this source is a direct mp4 file"
    assert media.call_count == 0
    assert not (tmp_path / "library").exists()


class FakeExtProvider(FakeProvider):
    def __init__(self, provider_id: str, ext: str) -> None:
        super().__init__(provider_id)
        self.ext = ext

    def resolve(self, asset: Asset) -> DownloadPlan:
        return DownloadPlan(
            asset=asset, method="http", url=asset.extra["download_url"], ext=self.ext
        )


@pytest.mark.parametrize("ext", ["mp3", ".MP3"])
@respx.mock
def test_download_ref_http_mp3_plan_accepts_mp3_as_noop(tmp_path: Path, ext: str) -> None:
    registry.register(FakeExtProvider("fakehttp", ext))
    cache_asset(make_asset().model_copy(update={"kind": Kind.AUDIO_SFX}))
    media = respx.get(MEDIA_URL).mock(return_value=httpx.Response(200, content=b"mp3 bytes"))

    url, result = service.download_ref("fakehttp:42", out=tmp_path, mp3=True)

    assert url == MEDIA_URL
    assert media.call_count == 1
    assert result.path.read_bytes() == b"mp3 bytes"
    assert result.path.suffix.lower() == ".mp3"


@pytest.mark.parametrize(
    ("kind", "ext"),
    [
        (Kind.VIDEO, "mp4"),
        (Kind.VIDEO, "webm"),
        (Kind.VIDEO, "bin"),  # unknown ext: the asset kind decides, ffmpeg uses video args
        (Kind.AUDIO_SFX, "mp3"),
        (Kind.AUDIO_MUSIC, "ogg"),
    ],
)
def test_download_ref_http_clip_dispatches_to_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind, ext: str
) -> None:
    from clipforage.download import service as dl

    registry.register(FakeExtProvider("fakehttp", ext))
    cache_asset(make_asset().model_copy(update={"kind": kind}))
    calls: list[tuple[DownloadPlan, tuple[float, float], Path | None]] = []
    fake_result = DownloadResult(
        path=tmp_path / "x.mp4", sidecar=tmp_path / "x.mp4.json", fetched_at=datetime.now(UTC)
    )

    def fake_clip(
        plan: DownloadPlan, clip: tuple[float, float], out_dir: Path | None = None, **kw: Any
    ) -> DownloadResult:
        calls.append((plan, clip, out_dir))
        return fake_result

    monkeypatch.setattr(dl, "download_http_clip", fake_clip)
    url, result = service.download_ref("fakehttp:42", out=tmp_path, clip=(2.0, 6.0))

    assert url == MEDIA_URL
    assert result is fake_result
    assert len(calls) == 1
    assert calls[0][0].ext == ext
    assert calls[0][1:] == ((2.0, 6.0), tmp_path)


def _capture_clip_kwargs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from clipforage.download import service as dl

    registry.register(FakeExtProvider("fakehttp", "mp4"))
    cache_asset(make_asset().model_copy(update={"kind": Kind.VIDEO}))
    seen: list[dict[str, Any]] = []
    fake_result = DownloadResult(
        path=tmp_path / "x.mp4", sidecar=tmp_path / "x.mp4.json", fetched_at=datetime.now(UTC)
    )

    def fake_clip(
        plan: DownloadPlan, clip: tuple[float, float], out_dir: Path | None = None, **kw: Any
    ) -> DownloadResult:
        seen.append(kw)
        return fake_result

    monkeypatch.setattr(dl, "download_http_clip", fake_clip)
    return seen


def test_download_ref_http_clip_forwards_explicit_max_wall_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_clip_kwargs(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "get_setting", lambda *a, **k: "999")  # explicit value wins
    service.download_ref("fakehttp:42", out=tmp_path, clip=(2.0, 6.0), max_wall_s=0)
    assert seen[0]["max_wall_s"] == 0


def test_download_ref_http_clip_falls_back_to_config_max_wall_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_clip_kwargs(tmp_path, monkeypatch)
    asked: list[tuple[Any, ...]] = []

    def fake_get_setting(section: str, key: str, env: str | None = None) -> str | None:
        asked.append((section, key, env))
        return "45.5"

    monkeypatch.setattr(service, "get_setting", fake_get_setting)
    service.download_ref("fakehttp:42", out=tmp_path, clip=(2.0, 6.0))
    assert asked == [("download", "clip_max_wall_s", "CLIP_MAX_WALL_S")]
    assert seen[0]["max_wall_s"] == 45.5


@pytest.mark.parametrize("raw", ["soon", "nan", "inf"])
def test_download_ref_ignores_bad_config_max_wall_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    seen = _capture_clip_kwargs(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "get_setting", lambda *a, **k: raw)
    with caplog.at_level("WARNING", logger="clipforage"):
        service.download_ref("fakehttp:42", out=tmp_path, clip=(2.0, 6.0))
    assert "max_wall_s" not in seen[0]  # download_http_clip keeps its 300 s default
    assert f"ignoring clip_max_wall_s {raw!r}" in caplog.text


@pytest.mark.parametrize(
    ("kind", "ext"),
    [
        (Kind.IMAGE, "jpg"),
        (Kind.GIF, "gif"),
        (Kind.STICKER, "mp4"),  # a video container, but not a video/audio asset
        (Kind.FONT, "ttf"),
        (Kind.LUT, "cube"),
    ],
)
@respx.mock
def test_download_ref_http_clip_rejects_non_media(tmp_path: Path, kind: Kind, ext: str) -> None:
    registry.register(FakeExtProvider("fakehttp", ext))
    cache_asset(make_asset().model_copy(update={"kind": kind}))
    media = respx.get(MEDIA_URL).mock(return_value=httpx.Response(200, content=b"x"))

    with pytest.raises(service.RefError) as info:
        service.download_ref("fakehttp:42", out=tmp_path / "library", clip=(1.0, 2.0))

    assert str(info.value) == "clip only applies to video/audio sources"
    assert media.call_count == 0
    assert not (tmp_path / "library").exists()


def make_result(tmp_path: Path) -> DownloadResult:
    media = tmp_path / "Rain on window [42].mp4"
    media.write_bytes(b"x")
    fetched = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    sidecar = write_sidecar(media, make_asset(), fetched, {"format": "mp4"})
    return DownloadResult(path=media, sidecar=sidecar, fetched_at=fetched)


def test_record_history_writes_row(tmp_path: Path) -> None:
    result = make_result(tmp_path)

    meta = service.record_history(MEDIA_URL, result)

    assert meta["license"] == "CC0-1.0"
    rows = service.history()
    assert len(rows) == 1
    row = rows[0]
    assert row["url"] == MEDIA_URL
    assert row["provider"] == "fakehttp"
    assert row["asset_id"] == "42"
    assert row["path"] == str(result.path)
    assert row["fetched_at"] == "2026-01-02T03:04:05+00:00"
    assert row["extra"]["format"] == "mp4"
    assert meta["history_id"] == row["id"]


def test_record_history_failure_warns_and_returns_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    result = make_result(tmp_path)

    def broken_cache(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(service, "Cache", broken_cache)
    with caplog.at_level("WARNING", logger="clipforage"):
        meta = service.record_history(MEDIA_URL, result)
    assert meta["provider"] == "fakehttp"
    assert meta["history_id"] is None
    assert "could not record download history: read-only file system" in caplog.text

    seen: list[str] = []
    service.record_history(MEDIA_URL, result, warn=seen.append)
    assert seen == ["could not record download history: read-only file system"]


def test_history_cache_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_cache(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(service, "Cache", broken_cache)
    with pytest.raises(service.CacheUnavailableError, match="cache unavailable: disk gone"):
        service.history()


def test_list_providers_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_KEYED_KEY", "k")
    registry.register(FakeProvider("keyed", requires_key=True))
    registry.register(FakeProvider("free"))
    registry.register(FakeProvider("nokey", requires_key=True))

    assert service.list_providers() == [
        {"id": "free", "kinds": ["video"], "requires_key": False, "has_key": False},
        {"id": "keyed", "kinds": ["video"], "requires_key": True, "has_key": True},
        {"id": "nokey", "kinds": ["video"], "requires_key": True, "has_key": False},
    ]


def test_search_unknown_provider_raises() -> None:
    registry.register(FakeProvider("fakehttp"))
    with pytest.raises(LookupError, match="unknown provider 'nope'"):
        service.search("rain", FILTERS, provider="nope")


def test_search_limit_and_provider() -> None:
    assets = [make_asset(str(i)) for i in range(5)]
    registry.register(FakeProvider("fakehttp", assets))
    registry.register(FakeProvider("other", [make_asset("x")]))

    only, _ = service.search("rain", FILTERS, provider="fakehttp")
    assert [a.id for a in only] == ["0", "1", "2", "3", "4"]
    found, errors = service.search("rain", FILTERS, provider="fakehttp", limit=2)
    assert errors == []
    assert [a.id for a in found] == ["0", "1"]


@pytest.mark.parametrize(
    ("lists", "expected"),
    [
        ([], []),
        ([[], []], []),
        ([[1, 2, 3]], [1, 2, 3]),
        ([[1, 2, 3], [10], [20, 21]], [1, 10, 20, 2, 21, 3]),
        ([[], [1, 2], [], [3]], [1, 3, 2]),
    ],
)
def test_interleave(lists: list[list[int]], expected: list[int]) -> None:
    assert service.interleave(lists) == expected


def make_provider_assets(provider_id: str, count: int) -> list[Asset]:
    return [
        make_asset(f"{provider_id}{i}").model_copy(update={"provider": provider_id})
        for i in range(count)
    ]


def test_search_interleaves_providers_before_limit() -> None:
    registry.register(FakeProvider("aa", make_provider_assets("aa", 5)))
    registry.register(FakeProvider("bb", make_provider_assets("bb", 2)))
    registry.register(FakeProvider("cc", make_provider_assets("cc", 5)))

    found, errors = service.search("rain", FILTERS, limit=6, use_cache=False)

    assert errors == []
    assert [a.id for a in found] == ["aa0", "bb0", "cc0", "aa1", "bb1", "cc1"]


def test_search_interleaves_cached_results() -> None:
    registry.register(FakeProvider("aa", make_provider_assets("aa", 3)))
    registry.register(FakeProvider("bb", make_provider_assets("bb", 3)))
    first, _ = service.search("rain", FILTERS, limit=4)
    registry.clear()
    registry.register(FakeProvider("aa"))  # now empty live; must come from cache
    registry.register(FakeProvider("bb"))

    cached, errors = service.search("rain", FILTERS, limit=4)

    assert errors == []
    assert [a.id for a in cached] == [a.id for a in first] == ["aa0", "bb0", "aa1", "bb1"]


@pytest.mark.parametrize(
    ("lists", "weights", "expected"),
    [
        ([], [], []),
        ([[], []], [1, 1], []),
        ([[], []], [2, 0], []),
        ([[1, 2, 3], [10], [20, 21]], [1, 1, 1], [1, 10, 20, 2, 21, 3]),
        ([[1, 2, 3, 4, 5], [10, 11, 12]], [2, 1], [1, 2, 10, 3, 4, 11, 5, 12]),
        ([[10, 11, 12], [1, 2, 3, 4, 5]], [1, 2], [1, 2, 10, 3, 4, 11, 5, 12]),
        ([[1, 2], [10, 11], [20, 21]], [1, 0, 1], [1, 20, 2, 21, 10, 11]),
        ([[10, 11, 12], [1], [20]], [0, 1, 0], [1, 10, 20, 11, 12]),
        ([[1], [10, 11, 12, 13, 14]], [3, 2], [1, 10, 11, 12, 13, 14]),
        ([[1, 2, 3, 4], [10]], [3, 1], [1, 2, 3, 10, 4]),
    ],
    ids=[
        "empty",
        "empty-lists",
        "empty-lists-demoted",
        "weights-1-1-1",
        "weights-2-1",
        "heavier-list-leads-round",
        "weight-0-appends-last",
        "only-demoted-tail-round-robin",
        "uneven-short-heavy",
        "uneven-long-heavy",
    ],
)
def test_interleave_weighted(
    lists: list[list[int]], weights: list[int], expected: list[int]
) -> None:
    assert service.interleave_weighted(lists, weights) == expected


@pytest.mark.parametrize(
    "lists",
    [[], [[1, 2, 3]], [[1, 2, 3], [10], [20, 21]], [[], [1, 2], [], [3]]],
)
def test_interleave_weighted_all_ones_equals_interleave(lists: list[list[int]]) -> None:
    assert service.interleave_weighted(lists, [1] * len(lists)) == service.interleave(lists)


def test_interleave_weighted_rejects_bad_weights() -> None:
    with pytest.raises(ValueError):
        service.interleave_weighted([[1], [2]], [1])
    with pytest.raises(ValueError):
        service.interleave_weighted([[1]], [-1])


@pytest.mark.parametrize(
    ("provider_id", "style", "expected"),
    [
        ("youtube", "meme", 3),
        ("pexels", "meme", 2),
        ("pixabay", "green_screen", 2),
        ("openverse", "meme", 1),
        ("wikimedia", "meme", 0),
        ("internet_archive", "green_screen", 0),
        ("klipy", "meme", 1),
        ("pexels", "cinematic", 3),
        ("pixabay", "cinematic", 3),
        ("youtube", "cinematic", 2),
        ("wikimedia", "cinematic", 1),
        ("internet_archive", "cinematic", 1),
        ("freesound", "cinematic", 1),
        ("youtube", "any", 1),
        ("wikimedia", "any", 1),
        ("internet_archive", "any", 0),
    ],
)
def test_provider_weight(provider_id: str, style: Style, expected: int) -> None:
    assert service.provider_weight(provider_id, style) == expected


@pytest.mark.parametrize(
    ("provider_id", "style", "kind", "expected"),
    [
        ("freesound", "any", Kind.AUDIO_SFX, 3),
        ("youtube", "meme", Kind.AUDIO_SFX, 2),  # audio ignores the meme style weight (3)
        ("openverse", "any", Kind.AUDIO_SFX, 2),
        ("internet_archive", "meme", Kind.AUDIO_SFX, 1),
        ("freesound", "any", Kind.AUDIO_MUSIC, 2),
        ("youtube", "any", Kind.AUDIO_MUSIC, 2),
        ("wikimedia", "meme", Kind.AUDIO_MUSIC, 1),
        ("youtube", "meme", Kind.VIDEO, 3),
    ],
)
def test_provider_weight_by_audio_kind(
    provider_id: str, style: Style, kind: Kind, expected: int
) -> None:
    assert service.provider_weight(provider_id, style, kind) == expected


def test_audio_search_ranks_cleared_sources_before_youtube() -> None:
    registry.register(QueryProvider("youtube", AUDIO, {"bonk": ["y1", "y2", "y3"]}))
    registry.register(QueryProvider("freesound", AUDIO, {"bonk": ["f1", "f2", "f3", "f4"]}))
    filters = SearchFilters(kind=Kind.AUDIO_SFX, style="meme")
    assets, _ = service.search("bonk", filters, use_cache=False, limit=7)
    assert _refs(assets) == [
        "freesound:f1",
        "freesound:f2",
        "freesound:f3",
        "youtube:y1",
        "youtube:y2",
        "freesound:f4",
        "youtube:y3",
    ]


def _register_style_providers() -> None:
    for provider_id in ("youtube", "wikimedia", "internet_archive", "pixabay"):
        registry.register(FakeProvider(provider_id, make_provider_assets(provider_id, 3)))


def test_search_meme_style_ranks_youtube_and_demotes_archives() -> None:
    _register_style_providers()
    filters = SearchFilters(kind=Kind.VIDEO, style="meme")

    found, errors = service.search("rain", filters, use_cache=False)

    assert errors == []
    # registry order is by id (internet_archive, pixabay, wikimedia, youtube); youtube (3)
    # leads each round ahead of pixabay (2); wikimedia / internet_archive (0) come last.
    assert [a.id for a in found] == [
        "youtube0",
        "youtube1",
        "youtube2",
        "pixabay0",
        "pixabay1",
        "pixabay2",
        "internet_archive0",
        "wikimedia0",
        "internet_archive1",
        "wikimedia1",
        "internet_archive2",
        "wikimedia2",
    ]
    assert [a.provider for a in found[:3]] == ["youtube"] * 3
    assert {a.provider for a in found[:6]} == {"youtube", "pixabay"}
    assert {a.provider for a in found[6:]} == {"wikimedia", "internet_archive"}


def test_search_meme_style_limit_drops_demoted() -> None:
    _register_style_providers()
    filters = SearchFilters(kind=Kind.VIDEO, style="meme")

    found, _ = service.search("rain", filters, limit=4, use_cache=False)

    assert {a.provider for a in found} <= {"youtube", "pixabay"}
    assert len(found) == 4


def test_search_any_style_keeps_round_robin_with_archive_last() -> None:
    _register_style_providers()
    filters = SearchFilters(kind=Kind.VIDEO, style="any")

    found, errors = service.search("rain", filters, use_cache=False)

    assert errors == []
    assert [a.id for a in found] == [
        *(f"{pid}{i}" for i in range(3) for pid in ("pixabay", "wikimedia", "youtube")),
        "internet_archive0",
        "internet_archive1",
        "internet_archive2",
    ]


def test_search_any_style_archive_after_other_providers() -> None:
    _register_style_providers()
    filters = SearchFilters(kind=Kind.VIDEO, style="any")

    found, _ = service.search("rain", filters, use_cache=False)
    capped, _ = service.search("rain", filters, limit=4, use_cache=False)

    providers = [a.provider for a in found]
    first_archive = providers.index("internet_archive")
    assert set(providers[first_archive:]) == {"internet_archive"}
    assert "internet_archive" not in {a.provider for a in capped}


def _shorts_asset(provider_id: str, asset_id: str, **update: object) -> Asset:
    return make_asset(asset_id).model_copy(update={"provider": provider_id, **update})


def test_search_shorts_puts_portrait_first_across_providers() -> None:
    registry.register(
        FakeProvider(
            "aa",
            [
                _shorts_asset("aa", "aa-land", duration_s=20.0, orientation="landscape"),
                _shorts_asset("aa", "aa-port", duration_s=25.0, orientation="portrait"),
            ],
        )
    )
    registry.register(
        FakeProvider(
            "bb",
            [
                _shorts_asset("bb", "bb-none", duration_s=15.0),
                _shorts_asset("bb", "bb-port-long", duration_s=120.0, orientation="portrait"),
                _shorts_asset("bb", "bb-port", duration_s=40.0, orientation="portrait"),
            ],
        )
    )
    filters = SearchFilters(kind=Kind.VIDEO, shorts=True)

    found, errors = service.search("rain", filters, use_cache=False)
    capped, _ = service.search("rain", filters, limit=2, use_cache=False)
    plain, _ = service.search("rain", SearchFilters(kind=Kind.VIDEO), use_cache=False)

    assert errors == []
    # Round-robin would give aa-land, bb-none, aa-port, bb-port; portrait moves up (stable)
    # and the 120 s portrait is dropped by the shorts cap.
    assert [a.id for a in found] == ["aa-port", "bb-port", "aa-land", "bb-none"]
    assert [a.id for a in capped] == ["aa-port", "bb-port"]
    assert [a.id for a in plain] == ["aa-land", "bb-none", "aa-port", "bb-port-long", "bb-port"]


def _register_shorts_providers(youtube_portrait: bool = False) -> None:
    # Registry order is by id: pixabay before youtube.
    registry.register(
        FakeProvider(
            "pixabay",
            [
                _shorts_asset("pixabay", "p-land", duration_s=20.0, orientation="landscape"),
                _shorts_asset("pixabay", "p-port", duration_s=30.0, orientation="portrait"),
            ],
        )
    )
    y1_orientation = "portrait" if youtube_portrait else None
    registry.register(
        FakeProvider(
            "youtube",
            [
                _shorts_asset("youtube", "y0", duration_s=15.0),
                _shorts_asset("youtube", "y1", duration_s=45.0, orientation=y1_orientation),
            ],
        )
    )


def test_search_shorts_puts_youtube_first_then_portrait_stock() -> None:
    _register_shorts_providers()

    found, errors = service.search(
        "rain", SearchFilters(kind=Kind.VIDEO, shorts=True), use_cache=False
    )
    plain, _ = service.search("rain", SearchFilters(kind=Kind.VIDEO), use_cache=False)

    assert errors == []
    # Style any is plain round-robin (p-land, y0, p-port, y1); shorts lifts YouTube to the
    # top in merged order, then portrait stock before landscape stock.
    assert [a.id for a in found] == ["y0", "y1", "p-port", "p-land"]
    assert [a.id for a in plain] == ["p-land", "y0", "p-port", "y1"]


def test_search_shorts_meme_style_puts_youtube_first() -> None:
    registry.register(
        FakeProvider(
            "pexels",
            [
                _shorts_asset("pexels", "x-land", duration_s=20.0, orientation="landscape"),
                _shorts_asset("pexels", "x-port", duration_s=30.0, orientation="portrait"),
                _shorts_asset("pexels", "x-port2", duration_s=35.0, orientation="portrait"),
            ],
        )
    )
    registry.register(
        FakeProvider(
            "youtube",
            [_shorts_asset("youtube", f"y{i}", duration_s=10.0 + i) for i in range(5)],
        )
    )
    filters = SearchFilters(kind=Kind.VIDEO, style="meme", shorts=True)

    found, _ = service.search("rain", filters, use_cache=False)
    plain, _ = service.search("rain", SearchFilters(kind=Kind.VIDEO, style="meme"), use_cache=False)

    assert [a.id for a in found] == ["y0", "y1", "y2", "y3", "y4", "x-port", "x-port2", "x-land"]
    assert [a.id for a in plain] == ["y0", "y1", "y2", "x-land", "x-port", "y3", "y4", "x-port2"]


def test_search_shorts_cinematic_keeps_portrait_first_across_providers() -> None:
    _register_shorts_providers(youtube_portrait=True)
    filters = SearchFilters(kind=Kind.VIDEO, style="cinematic", shorts=True)

    found, errors = service.search("rain", filters, use_cache=False)
    plain, _ = service.search(
        "rain", SearchFilters(kind=Kind.VIDEO, style="cinematic"), use_cache=False
    )

    assert errors == []
    # Cinematic: pixabay (3) leads youtube (2) each round -> p-land, p-port, y0, y1; shorts
    # sorts portrait first across providers (stable), with no YouTube grouping.
    assert [a.id for a in plain] == ["p-land", "p-port", "y0", "y1"]
    assert [a.id for a in found] == ["p-port", "y1", "p-land", "y0"]


def test_search_cache_unavailable_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    registry.register(FakeProvider("fakehttp", [make_asset()]))

    def broken_cache(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(service, "Cache", broken_cache)
    seen: list[str] = []
    found, errors = service.search("rain", FILTERS, warn=seen.append)

    assert [a.id for a in found] == ["42"]
    assert errors == []
    assert seen == ["cache unavailable: disk gone"]


# --- quality ----------------------------------------------------------------

YT_URL = "https://www.youtube.com/watch?v=abc123"


class FakeYtdlpProvider(FakeProvider):
    def resolve(self, asset: Asset) -> DownloadPlan:
        return DownloadPlan(asset=asset, method="ytdlp", url=YT_URL, ext="mp4")


def _capture_download_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    registry.register(FakeYtdlpProvider("fakehttp"))
    cache_asset(make_asset())
    seen: list[dict[str, Any]] = []
    fake_result = DownloadResult(
        path=tmp_path / "x.mp4", sidecar=tmp_path / "x.mp4.json", fetched_at=datetime.now(UTC)
    )

    def fake_plan(plan: DownloadPlan, out: Path | None = None, **kw: Any) -> DownloadResult:
        seen.append(kw)
        return fake_result

    monkeypatch.setattr(service, "download_plan", fake_plan)
    return seen


def test_download_ref_forwards_explicit_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_download_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "get_setting", lambda *a, **k: "360")  # explicit value wins
    service.download_ref("fakehttp:42", out=tmp_path, quality="720")
    assert seen[0]["quality"] == "720"


def test_download_ref_explicit_best_overrides_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_download_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "get_setting", lambda *a, **k: "720")
    service.download_ref("fakehttp:42", out=tmp_path, quality="best")
    assert seen[0]["quality"] == "best"


def test_download_ref_falls_back_to_config_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_download_plan(tmp_path, monkeypatch)
    asked: list[tuple[Any, ...]] = []

    def fake_get_setting(section: str, key: str, env: str | None = None) -> str | None:
        asked.append((section, key, env))
        return "1080"

    monkeypatch.setattr(service, "get_setting", fake_get_setting)
    service.download_ref("fakehttp:42", out=tmp_path)
    assert asked == [("download", "quality", "QUALITY")]
    assert seen[0]["quality"] == "1080"


def test_download_ref_no_quality_setting_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_download_plan(tmp_path, monkeypatch)
    service.download_ref("fakehttp:42", out=tmp_path)
    assert seen[0]["quality"] is None


def test_download_ref_quality_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_download_plan(tmp_path, monkeypatch)
    monkeypatch.setenv("CLIPFORAGE_QUALITY", "480")
    service.download_ref("fakehttp:42", out=tmp_path)
    assert seen[0]["quality"] == "480"


@pytest.mark.parametrize(("explicit", "configured"), [("999", None), (None, "720p")])
def test_download_ref_invalid_quality_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: str | None, configured: str | None
) -> None:
    seen = _capture_download_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "get_setting", lambda *a, **k: configured)
    with pytest.raises(service.RefError) as info:
        service.download_ref("fakehttp:42", out=tmp_path, quality=explicit)
    assert "unknown quality" in str(info.value)
    assert "best, 2160, 1440, 1080, 720, 480, 360" in info.value.hint
    assert seen == []


class FakeYtdlpAudioProvider(FakeProvider):
    def resolve(self, asset: Asset) -> DownloadPlan:
        return DownloadPlan(asset=asset, method="ytdlp", url=YT_URL, ext="mp3")


@pytest.mark.parametrize("clip", [None, (1.0, 2.0)])
def test_download_ref_audio_ytdlp_plan_stays_mp3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip: tuple[float, float] | None
) -> None:
    registry.register(FakeYtdlpAudioProvider("fakehttp"))
    cache_asset(make_asset())
    fake_result = DownloadResult(
        path=tmp_path / "x.mp3", sidecar=tmp_path / "x.mp3.json", fetched_at=datetime.now(UTC)
    )
    audio_only: list[bool] = []

    def fake_url(url: str, out: Path | None = None, **kw: Any) -> DownloadResult:
        audio_only.append(kw["audio_only"])
        return fake_result

    def fake_plan(plan: DownloadPlan, out: Path | None = None, **kw: Any) -> DownloadResult:
        audio_only.append(plan.ext == "mp3")  # real download_plan maps ext mp3 -> audio_only
        return fake_result

    monkeypatch.setattr(service, "download_url", fake_url)
    monkeypatch.setattr(service, "download_plan", fake_plan)
    service.download_ref("fakehttp:42", out=tmp_path, clip=clip)
    assert audio_only == [True]


@respx.mock
def test_download_ref_http_plan_ignores_config_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.register(FakeProvider("fakehttp"))
    cache_asset(make_asset())
    respx.get(MEDIA_URL).mock(return_value=httpx.Response(200, content=b"mp4 bytes"))
    monkeypatch.setenv("CLIPFORAGE_QUALITY", "bogus")  # stock files never read the setting
    _, result = service.download_ref("fakehttp:42", out=tmp_path)
    assert result.path.read_bytes() == b"mp4 bytes"


def test_download_link_quality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    fake_result = DownloadResult(
        path=tmp_path / "x.mp4", sidecar=tmp_path / "x.mp4.json", fetched_at=datetime.now(UTC)
    )

    def fake_url(url: str, out: Path | None = None, **kw: Any) -> DownloadResult:
        seen.append(kw)
        return fake_result

    monkeypatch.setattr(service, "download_url", fake_url)
    service.download_link(YT_URL, out=tmp_path, quality="720")
    monkeypatch.setattr(service, "get_setting", lambda *a, **k: "360")
    service.download_link(YT_URL, out=tmp_path)
    assert [kw["quality"] for kw in seen] == ["720", "360"]
    with pytest.raises(service.RefError):
        service.download_link(YT_URL, out=tmp_path, quality="999")
    assert len(seen) == 2


# --- search_expanded: recall expansions, dedupe, related audio kind, merged errors ----------


class QueryProvider:
    """Returns assets derived from the query and kind; records every call."""

    requires_key = False

    def __init__(
        self,
        provider_id: str,
        kinds: frozenset[Kind],
        per_query: dict[str, list[str]] | None = None,
        fail: bool = False,
    ) -> None:
        self.id = provider_id
        self.kinds = kinds
        self.per_query = per_query or {}
        self.fail = fail
        self.calls: list[tuple[str, Kind, int | None]] = []

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        self.calls.append((query, kind, filters.limit))
        if self.fail:
            raise ProviderError(self.id, "down")
        return [
            Asset(
                provider=self.id,
                id=asset_id,
                kind=kind,
                title=f"{query} {asset_id}",
                source_url=f"https://example.com/{self.id}/{asset_id}",
                license="CC0-1.0",
                attribution_text="CC0",
                commercial_ok=True,
            )
            for asset_id in self.per_query.get(query, [])
        ]

    def resolve(self, asset: Asset) -> DownloadPlan:
        raise NotImplementedError


VIDEO_ONLY = frozenset({Kind.VIDEO})
AUDIO = frozenset({Kind.AUDIO_SFX, Kind.AUDIO_MUSIC})


def _refs(assets: list[Asset]) -> list[str]:
    return [f"{a.provider}:{a.id}" for a in assets]


def test_search_expanded_adds_unseen_expansion_results() -> None:
    fake = QueryProvider(
        "fake",
        VIDEO_ONLY,
        {"dog in burning room": ["a", "b"], "this is fine": ["b", "c", "d"]},
    )
    registry.register(fake)
    outcome = service.search_expanded("dog in burning room", FILTERS, limit=10, use_cache=False)
    assert outcome.expanded_queries == ["this is fine"]
    assert _refs(outcome.assets) == ["fake:a", "fake:b", "fake:c", "fake:d"]
    assert ("this is fine", Kind.VIDEO, 5) in fake.calls  # max(10 // 2, 5)
    assert outcome.related == [] and outcome.related_kind is None
    assert outcome.errors == []


def test_search_expanded_caps_at_twice_the_limit() -> None:
    fake = QueryProvider(
        "fake",
        VIDEO_ONLY,
        {"dog in burning room": ["a"], "this is fine": [str(i) for i in range(20)]},
    )
    registry.register(fake)
    outcome = service.search_expanded("dog in burning room", FILTERS, limit=2, use_cache=False)
    assert len(outcome.assets) == 4
    assert outcome.assets[0].id == "a"


def test_search_expanded_dedupes_primary() -> None:
    registry.register(QueryProvider("fake", VIDEO_ONLY, {"rain": ["a", "a", "b"]}))
    outcome = service.search_expanded("rain", FILTERS, limit=10, use_cache=False)
    assert _refs(outcome.assets) == ["fake:a", "fake:b"]
    assert outcome.expanded_queries == []  # nothing in the alias data matches "rain"


def test_search_expanded_respects_expand_false() -> None:
    fake = QueryProvider("fake", VIDEO_ONLY, {"dog in burning room": ["a"], "this is fine": ["c"]})
    registry.register(fake)
    filters = SearchFilters(kind=Kind.VIDEO, expand=False)
    outcome = service.search_expanded("dog in burning room", filters, limit=10, use_cache=False)
    assert outcome.expanded_queries == []
    assert _refs(outcome.assets) == ["fake:a"]
    assert [call[0] for call in fake.calls] == ["dog in burning room"]


def test_search_expanded_skips_expansion_when_full_and_plain_video() -> None:
    fake = QueryProvider("fake", VIDEO_ONLY, {"dog in burning room": ["a", "b"]})
    registry.register(fake)
    outcome = service.search_expanded("dog in burning room", FILTERS, limit=2, use_cache=False)
    assert outcome.expanded_queries == []
    meme = SearchFilters(kind=Kind.VIDEO, style="meme")
    outcome = service.search_expanded("dog in burning room", meme, limit=2, use_cache=False)
    assert outcome.expanded_queries == ["this is fine"]  # meme style always expands


def test_search_expanded_runs_related_audio_kind() -> None:
    fake = QueryProvider("fake", AUDIO, {"undertale bgm": ["m1", "m2"]})
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC)
    outcome = service.search_expanded("undertale bgm", filters, limit=5, use_cache=False)
    assert outcome.related_kind == Kind.AUDIO_SFX
    assert ("undertale bgm", Kind.AUDIO_SFX, service.RELATED_LIMIT) in fake.calls
    # The fake returns the same ids for both kinds, so related is deduped against assets.
    assert outcome.related == []
    assert "undertale soundtrack" in outcome.expanded_queries  # audio kinds always expand


def test_search_expanded_related_keeps_new_results_and_respects_related_false() -> None:
    class KindAware(QueryProvider):
        def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
            prefix = "sfx" if kind == Kind.AUDIO_SFX else "mus"
            self.per_query = {query: [f"{prefix}1"]}
            return super().search(query, kind, filters)

    registry.register(KindAware("fake", AUDIO))
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False)
    outcome = service.search_expanded("rain", filters, limit=5, use_cache=False)
    assert _refs(outcome.assets) == ["fake:mus1"]
    assert _refs(outcome.related) == ["fake:sfx1"]
    assert outcome.related[0].kind == Kind.AUDIO_SFX
    off = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False, related=False)
    outcome = service.search_expanded("rain", off, limit=5, use_cache=False)
    assert outcome.related == [] and outcome.related_kind is None


def test_search_expanded_related_uses_core_query() -> None:
    fake = QueryProvider("fake", AUDIO, {"undertale bgm": ["m1"], "undertale": ["s1", "s2"]})
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False)
    outcome = service.search_expanded("undertale bgm", filters, limit=5, use_cache=False)
    related_calls = [c[0] for c in fake.calls if c[1] == Kind.AUDIO_SFX]
    # core query found results, so the raw query is not retried; one-word core + sfx gets
    # the "sound effect" variant too.
    assert related_calls == ["undertale", "undertale sound effect"]
    assert _refs(outcome.related) == ["fake:s1", "fake:s2"]


def test_search_expanded_related_falls_back_to_raw_query_once() -> None:
    fake = QueryProvider("fake", AUDIO, {"undertale bgm": ["m1"]})
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False)
    service.search_expanded("undertale bgm", filters, limit=5, use_cache=False)
    related_calls = [c[0] for c in fake.calls if c[1] == Kind.AUDIO_SFX]
    assert related_calls == ["undertale", "undertale bgm", "undertale sound effect"]


def test_search_expanded_related_music_has_no_sound_effect_variant() -> None:
    fake = QueryProvider("fake", AUDIO, {"undertale sfx": ["s1"]})
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_SFX, expand=False)
    service.search_expanded("undertale sfx", filters, limit=5, use_cache=False)
    related_calls = [c[0] for c in fake.calls if c[1] == Kind.AUDIO_MUSIC]
    assert related_calls == ["undertale", "undertale sfx"]


def test_search_expanded_related_multi_word_core_skips_variant_and_same_raw() -> None:
    fake = QueryProvider("fake", AUDIO)
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False)
    service.search_expanded("vine boom", filters, limit=5, use_cache=False)
    related_calls = [c[0] for c in fake.calls if c[1] == Kind.AUDIO_SFX]
    assert related_calls == ["vine boom"]  # core == raw: no retry; two words: no variant


def test_search_expanded_related_capped_at_related_limit() -> None:
    ids = [f"s{i}" for i in range(5)]
    more = [f"t{i}" for i in range(5)]
    fake = QueryProvider("fake", AUDIO, {"undertale": ids, "undertale sound effect": more})
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False)
    outcome = service.search_expanded("undertale bgm", filters, limit=5, use_cache=False)
    assert len(outcome.related) == service.RELATED_LIMIT
    assert _refs(outcome.related)[-1] == "fake:t0"


def test_search_expanded_expands_raw_and_core_query(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def fake_expand(query: str, kind: Kind | None = None) -> list[str]:
        asked.append(query)
        return {"undertale bgm": ["a", "b"], "undertale": ["b", "c", "d"]}.get(query, [])

    monkeypatch.setattr(service.recall, "expand_query", fake_expand)
    fake = QueryProvider("fake", AUDIO)
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, related=False)
    outcome = service.search_expanded("undertale bgm", filters, limit=5, use_cache=False)
    assert asked == ["undertale bgm", "undertale"]
    assert outcome.expanded_queries == ["a", "b", "c"]  # union, deduped, max 3


def test_search_expanded_cleared_only_filters_before_truncation_and_related() -> None:
    class Mixed(QueryProvider):
        def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
            prefix = "sfx" if kind == Kind.AUDIO_SFX else "mus"
            lic = ["unknown", "CC BY-NC 4.0", "CC0-1.0", "CC BY 4.0"]
            return [
                Asset(
                    provider=self.id,
                    id=f"{prefix}{i}",
                    kind=kind,
                    title=f"t{i}",
                    source_url=f"https://example.com/{prefix}{i}",
                    license=license_,
                    attribution_text="x",
                    commercial_ok=None,
                )
                for i, license_ in enumerate(lic)
            ]

    registry.register(Mixed("fake", AUDIO))
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False)
    outcome = service.search_expanded("rain", filters, limit=2, use_cache=False, cleared_only=True)
    # Truncating first would have kept only the unknown + NC items and filtered them away.
    assert _refs(outcome.assets) == ["fake:mus2", "fake:mus3"]
    assert _refs(outcome.related) == ["fake:sfx2", "fake:sfx3"]
    unfiltered = service.search_expanded("rain", filters, limit=2, use_cache=False)
    assert _refs(unfiltered.assets) == ["fake:mus0", "fake:mus1"]


def test_search_expanded_single_provider_without_related_kind_skips_related() -> None:
    music_only = QueryProvider("music", frozenset({Kind.AUDIO_MUSIC}), {"rain": ["a"]})
    registry.register(music_only)
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False)
    outcome = service.search_expanded("rain", filters, provider="music", use_cache=False)
    assert outcome.related_kind is None
    assert [call[1] for call in music_only.calls] == [Kind.AUDIO_MUSIC]


def test_search_expanded_collects_errors_once() -> None:
    registry.register(QueryProvider("ok", VIDEO_ONLY, {"this is fine": ["x"]}))
    registry.register(QueryProvider("broken", VIDEO_ONLY, fail=True))
    outcome = service.search_expanded("dog in burning room", FILTERS, limit=10, use_cache=False)
    assert outcome.expanded_queries == ["this is fine"]
    assert _refs(outcome.assets) == ["ok:x"]
    assert [(e.provider, e.message) for e in outcome.errors] == [("broken", "down")]


def test_search_expanded_toggles_do_not_reach_providers() -> None:
    seen: list[SearchFilters] = []

    class Recording(QueryProvider):
        def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
            seen.append(filters)
            return []

    registry.register(Recording("rec", VIDEO_ONLY))
    filters = SearchFilters(kind=Kind.VIDEO, expand=False, related=False)
    service.search_expanded("rain", filters, limit=3, use_cache=False)
    assert seen and all(f.expand and f.related for f in seen)


# --- search_expanded: related noise filter and off-topic primary demotion ------------------


class TitledProvider(QueryProvider):
    """Returns fixed ``(id, title, duration_s)`` assets per kind; records the filters it got."""

    def __init__(
        self, provider_id: str, per_kind: dict[Kind, list[tuple[str, str, float | None]]]
    ) -> None:
        super().__init__(provider_id, frozenset(per_kind))
        self.per_kind = per_kind
        self.seen: list[SearchFilters] = []

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        self.seen.append(filters)
        return [
            Asset(
                provider=self.id,
                id=asset_id,
                kind=kind,
                title=title,
                duration_s=duration,
                source_url=f"https://example.com/{self.id}/{asset_id}",
                license="CC0-1.0",
                attribution_text="CC0",
                commercial_ok=True,
            )
            for asset_id, title, duration in self.per_kind.get(kind, [])
        ]


def test_search_expanded_related_music_drops_long_form_noise() -> None:
    fake = TitledProvider(
        "fake",
        {
            Kind.AUDIO_SFX: [("s1", "Undertale encounter", 1.0)],
            Kind.AUDIO_MUSIC: [
                ("mix", "Undertale DJ Mix 2023", 7200.0),
                ("book", "Undertale: The Audiobook", None),
                ("tenh", "Megalovania 10 Hours", None),
                ("long", "Undertale OST medley", 720.0),
                ("track", "Megalovania", 180.0),
                ("nolen", "Hopes and Dreams", None),
            ],
        },
    )
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_SFX, expand=False)
    outcome = service.search_expanded("undertale", filters, limit=5, use_cache=False)
    assert _refs(outcome.assets) == ["fake:s1"]
    assert outcome.related_kind == Kind.AUDIO_MUSIC
    assert _refs(outcome.related) == ["fake:track", "fake:nolen"]
    music = [f for f in fake.seen if f.kind == Kind.AUDIO_MUSIC]
    assert music and all(f.max_duration == 600.0 for f in music)


def test_search_expanded_related_sfx_capped_at_60s() -> None:
    fake = TitledProvider(
        "fake",
        {
            Kind.AUDIO_MUSIC: [("m1", "Vine boom remix", 120.0)],
            Kind.AUDIO_SFX: [
                ("short", "Vine boom", 2.0),
                ("long", "Vine boom compilation", 90.0),
                ("edge", "Vine boom extended", 60.0),
                ("nolen", "Vine boom bass", None),
            ],
        },
    )
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False)
    outcome = service.search_expanded("vine boom", filters, limit=5, use_cache=False)
    assert _refs(outcome.related) == ["fake:short", "fake:edge", "fake:nolen"]
    sfx = [f for f in fake.seen if f.kind == Kind.AUDIO_SFX]
    assert sfx and all(f.max_duration == 60.0 for f in sfx)


def test_search_expanded_related_keeps_tighter_user_max_duration() -> None:
    fake = TitledProvider("fake", {Kind.AUDIO_SFX: [], Kind.AUDIO_MUSIC: []})
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_SFX, expand=False, max_duration=30)
    service.search_expanded("undertale", filters, limit=5, use_cache=False)
    music = [f for f in fake.seen if f.kind == Kind.AUDIO_MUSIC]
    assert music and all(f.max_duration == 30 for f in music)


def test_search_expanded_noise_filter_leaves_primary_and_no_related_alone() -> None:
    fake = TitledProvider(
        "fake",
        {
            Kind.AUDIO_SFX: [("s1", "Undertale sfx", 1.0)],
            Kind.AUDIO_MUSIC: [("mix", "Undertale DJ Mix", 7200.0), ("t", "Undertale", 90.0)],
        },
    )
    registry.register(fake)
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False, related=False)
    outcome = service.search_expanded("undertale", filters, limit=5, use_cache=False)
    assert _refs(outcome.assets) == ["fake:mix", "fake:t"]  # primary is not noise-filtered
    assert outcome.related == [] and outcome.related_kind is None
    assert all(f.kind == Kind.AUDIO_MUSIC and f.max_duration is None for f in fake.seen)


@pytest.mark.parametrize(
    "title",
    [
        "Lofi DJ set",
        "mixtape vol 2",
        "Full Album",
        "2 hours of rain",
        "Chill 3hrs",
        "sleep 8h",
        "Live set at Boiler Room",
        "Weekly podcast #3",
        "Radio Show ep. 4",
        "Audio Book",
    ],
)
def test_related_noise_patterns_match(title: str) -> None:
    assert service.relevance.is_related_noise(title)


@pytest.mark.parametrize("title", ["Megalovania", "Shower thoughts", "Hour of the wolf", "DJ"])
def test_related_noise_patterns_spare_normal_titles(title: str) -> None:
    assert not service.relevance.is_related_noise(title)


PRIMARY_MUSIC = [
    ("finale", "Finale", 113.0),
    ("hopes", "Hopes And Dreams (UNDERTALE Soundtrack)", 185.0),
    ("long", "Rain on a tin roof", 2400.0),
    ("pod", "Nerd Noise Radio Podcast", 200.0),
    ("nolen", "Episode 33 complete", None),
    ("track", "MEGALOVANIA", 180.0),
    ("theme", "undertale's theme", 120.0),
]


def test_search_expanded_demotes_only_noise_or_long_off_topic_audio_primaries() -> None:
    registry.register(TitledProvider("fake", {Kind.AUDIO_MUSIC: PRIMARY_MUSIC}))
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False, related=False)
    outcome = service.search_expanded("undertale bgm", filters, limit=10, use_cache=False)
    # Short zero-overlap tracks (finale 113 s, MEGALOVANIA 180 s) keep their position; the
    # 2400 s and unknown-length zero-overlap items and the podcast title go last, in order.
    assert _refs(outcome.assets) == [
        "fake:finale",
        "fake:hopes",
        "fake:track",
        "fake:theme",
        "fake:long",
        "fake:pod",
        "fake:nolen",
    ]


def test_search_expanded_demotes_noise_title_even_when_it_shares_a_token() -> None:
    items = [("pod", "Undertale podcast ep 4", 30.0), ("t", "Undertale", 90.0)]
    registry.register(TitledProvider("fake", {Kind.AUDIO_MUSIC: items}))
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False, related=False)
    outcome = service.search_expanded("undertale bgm", filters, limit=10, use_cache=False)
    assert _refs(outcome.assets) == ["fake:t", "fake:pod"]


def test_search_expanded_sfx_demotion_uses_60s_cap() -> None:
    items = [("long", "Thunder", 90.0), ("short", "Crash", 2.0), ("hit", "Vine boom", 1.0)]
    registry.register(TitledProvider("fake", {Kind.AUDIO_SFX: items}))
    filters = SearchFilters(kind=Kind.AUDIO_SFX, expand=False, related=False)
    outcome = service.search_expanded("vine boom", filters, limit=10, use_cache=False)
    assert _refs(outcome.assets) == ["fake:short", "fake:hit", "fake:long"]


@pytest.mark.parametrize(
    ("query", "kind"),
    [("undertale", Kind.AUDIO_MUSIC), ("bgm music", Kind.AUDIO_MUSIC), ("undertale bgm", None)],
)
def test_search_expanded_demotion_skipped(query: str, kind: Kind | None) -> None:
    """One-word queries, stopword-only queries and non-audio kinds keep the merge order."""
    target = kind or Kind.VIDEO
    registry.register(TitledProvider("fake", {target: PRIMARY_MUSIC}))
    filters = SearchFilters(kind=target, expand=False, related=False)
    outcome = service.search_expanded(query, filters, limit=10, use_cache=False)
    assert _refs(outcome.assets) == [f"fake:{i}" for i, _, _ in PRIMARY_MUSIC]


OPENVERSE_FIXTURES = Path(__file__).parent / "fixtures" / "openverse"


@respx.mock
def test_cleared_only_music_search_reaches_openverse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIPFORAGE_OPENVERSE_KEY", raising=False)
    cleared = json.loads((OPENVERSE_FIXTURES / "audio_music_cleared.json").read_text("utf-8"))
    mixed = json.loads((OPENVERSE_FIXTURES / "audio_search.json").read_text("utf-8"))

    def respond(request: httpx.Request) -> httpx.Response:
        # The default page is non-commercial Jamendo music; only the license filter clears it.
        payload = cleared if "license" in request.url.params else mixed
        return httpx.Response(200, json=payload)

    route = respx.get(OPENVERSE_AUDIO_URL).mock(side_effect=respond)
    registry.register(OpenverseProvider(client=httpx.Client()))
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC)

    found, errors = service.search("lofi", filters, use_cache=False, cleared_only=True)

    assert errors == []
    assert route.calls.last.request.url.params["license"] == "cc0,pdm,by,by-sa"
    assert [a.id for a in found] == [hit["id"] for hit in cleared["results"]]
    assert all(rights.is_cleared(rights.classify(a).level) for a in found)
    plain, _ = service.search("lofi", filters, use_cache=False)
    assert "license" not in route.calls.last.request.url.params
    assert {a.license for a in plain} == {"CC0 1.0", "CC BY-NC 3.0"}


RELAX_FILTERS = SearchFilters(kind=Kind.AUDIO_MUSIC, expand=False, related=False)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("phonk background music", "phonk"),
        ("Royalty Free lofi BEATS", "lofi"),
        ("door slam sound effect", "door slam"),
        ("whoosh sound-effects sfx", "whoosh"),
        ("  epic   instrumental track ", "epic"),
        ("background music", ""),
        ("phonk", "phonk"),
    ],
)
def test_relax_query(query: str, expected: str) -> None:
    assert service.relax_query(query) == expected


def test_cleared_only_audio_retries_relaxed_query_when_empty() -> None:
    fake = QueryProvider("fake", AUDIO, {"phonk": ["p1", "p2", "p3"]})
    registry.register(fake)
    outcome = service.search_expanded(
        "phonk background music", RELAX_FILTERS, limit=10, use_cache=False, cleared_only=True
    )
    assert outcome.relaxed_query == "phonk"
    assert _refs(outcome.assets) == ["fake:p1", "fake:p2", "fake:p3"]
    assert [c[0] for c in fake.calls] == ["phonk background music", "phonk"]


def test_no_relaxed_retry_without_cleared_only() -> None:
    fake = QueryProvider("fake", AUDIO, {"phonk": ["p1"]})
    registry.register(fake)
    outcome = service.search_expanded(
        "phonk background music", RELAX_FILTERS, limit=10, use_cache=False
    )
    assert outcome.relaxed_query is None
    assert outcome.assets == []
    assert [c[0] for c in fake.calls] == ["phonk background music"]


def test_no_relaxed_retry_when_primary_has_results() -> None:
    fake = QueryProvider("fake", AUDIO, {"phonk background music": ["a"], "phonk": ["p1"]})
    registry.register(fake)
    outcome = service.search_expanded(
        "phonk background music", RELAX_FILTERS, limit=10, use_cache=False, cleared_only=True
    )
    assert outcome.relaxed_query is None
    assert _refs(outcome.assets) == ["fake:a"]
    assert [c[0] for c in fake.calls] == ["phonk background music"]


@pytest.mark.parametrize("query", ["background music", "phonk"])
def test_no_relaxed_retry_when_relaxed_is_empty_or_same(query: str) -> None:
    fake = QueryProvider("fake", AUDIO, {})
    registry.register(fake)
    outcome = service.search_expanded(
        query, RELAX_FILTERS, limit=10, use_cache=False, cleared_only=True
    )
    assert outcome.relaxed_query is None
    assert [c[0] for c in fake.calls] == [query]


def test_no_relaxed_retry_for_video() -> None:
    fake = QueryProvider("fake", VIDEO_ONLY, {"phonk": ["p1"]})
    registry.register(fake)
    filters = SearchFilters(kind=Kind.VIDEO, expand=False)
    outcome = service.search_expanded(
        "phonk background music", filters, limit=10, use_cache=False, cleared_only=True
    )
    assert outcome.relaxed_query is None
    assert [c[0] for c in fake.calls] == ["phonk background music"]
