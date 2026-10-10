import json
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote_plus

import pytest
import yt_dlp

from clipforage.models import Kind, SearchFilters
from clipforage.providers import registry
from clipforage.providers.base import Provider, ProviderError
from clipforage.providers.youtube import YouTubeProvider

FIXTURE = Path(__file__).parent / "fixtures" / "youtube" / "search_green_screen.json"
VIDEO_FILTERS = SearchFilters(kind=Kind.VIDEO)


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FakeYDL:
    def __init__(
        self, opts: dict[str, Any], result: Any = None, exc: Exception | None = None
    ) -> None:
        self.opts = opts
        self.result = result
        self.exc = exc
        self.queries: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> Any:
        self.queries.append(url)
        assert download is False
        if self.exc is not None:
            raise self.exc
        return self.result


class FakeFactory:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.instances: list[FakeYDL] = []

    def __call__(self, opts: dict[str, Any]) -> FakeYDL:
        ydl = FakeYDL(opts, self.result, self.exc)
        self.instances.append(ydl)
        return ydl


@pytest.fixture
def factory() -> FakeFactory:
    return FakeFactory(result=load_fixture())


def test_query_and_opts(factory: FakeFactory) -> None:
    YouTubeProvider(ydl_factory=factory).search("green screen meme", Kind.VIDEO, VIDEO_FILTERS)
    assert len(factory.instances) == 1
    ydl = factory.instances[0]
    assert ydl.queries == ["ytsearch30:green screen meme"]
    assert "playlistend" not in ydl.opts
    assert ydl.opts["extract_flat"] == "in_playlist"
    assert ydl.opts["skip_download"] is True


def test_cookies_from_browser_in_opts(factory: FakeFactory) -> None:
    cookies = ("firefox", None, None, None)
    provider = YouTubeProvider(ydl_factory=factory, cookies_from_browser=cookies)
    provider.search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert factory.instances[0].opts["cookiesfrombrowser"] == cookies


def test_no_cookies_by_default(factory: FakeFactory) -> None:
    YouTubeProvider(ydl_factory=factory).search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert "cookiesfrombrowser" not in factory.instances[0].opts


def test_live_and_null_entries_skipped(factory: FakeFactory) -> None:
    assets = YouTubeProvider(ydl_factory=factory).search(
        "green screen meme", Kind.VIDEO, VIDEO_FILTERS
    )
    assert [a.id for a in assets] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_normal_entry_mapping(factory: FakeFactory) -> None:
    asset = YouTubeProvider(ydl_factory=factory).search("q", Kind.VIDEO, VIDEO_FILTERS)[0]
    assert asset.provider == "youtube"
    assert asset.kind == Kind.VIDEO
    assert asset.title == "Fake Green Screen Surprised Cat"
    assert asset.source_url == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert asset.license == "unknown"
    assert asset.commercial_ok is None
    assert "Fake Chroma Channel" in asset.attribution_text
    assert "Third-party" in asset.attribution_text
    assert asset.preview_url == "https://i.ytimg.com/vi/aaaaaaaaaaa/hqdefault.jpg?large"
    assert asset.duration_s == 12.0
    assert asset.orientation is None
    assert asset.extra == {
        "channel": "Fake Chroma Channel",
        "channel_url": "https://www.youtube.com/channel/UCfakechannel00000000001",
        "view_count": 12345,
        "uploader_id": "@fakechroma",
        "search_query": "q",
    }


def test_shorts_entry_canonical_url_and_portrait(factory: FakeFactory) -> None:
    shorts = YouTubeProvider(ydl_factory=factory).search("q", Kind.VIDEO, VIDEO_FILTERS)[1]
    assert shorts.source_url == "https://www.youtube.com/watch?v=bbbbbbbbbbb"
    assert shorts.orientation == "portrait"
    assert shorts.duration_s == 9.0
    assert "unknown channel" in shorts.attribution_text
    assert shorts.license == "unknown"
    assert shorts.commercial_ok is None


def test_entry_without_id_is_skipped() -> None:
    valid = load_fixture()["entries"][0]
    factory = FakeFactory(result={"entries": [{"_type": "url", "title": "no id"}, valid]})
    assets = YouTubeProvider(ydl_factory=factory).search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert [a.id for a in assets] == [valid["id"]]


def test_download_error_becomes_provider_error() -> None:
    factory = FakeFactory(exc=yt_dlp.utils.DownloadError("boom"))
    with pytest.raises(ProviderError) as info:
        YouTubeProvider(ydl_factory=factory).search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert info.value.provider == "youtube"
    assert "DownloadError" in info.value.message


def test_other_exception_becomes_provider_error() -> None:
    factory = FakeFactory(exc=RuntimeError("kaput"))
    with pytest.raises(ProviderError) as info:
        YouTubeProvider(ydl_factory=factory).search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert info.value.provider == "youtube"
    assert info.value.message == "RuntimeError: kaput"


def test_satisfies_protocol() -> None:
    assert isinstance(YouTubeProvider(), Provider)


def test_resolve(factory: FakeFactory) -> None:
    provider = YouTubeProvider(ydl_factory=factory)
    asset = provider.search("q", Kind.VIDEO, VIDEO_FILTERS)[0]
    plan = provider.resolve(asset)
    assert plan.method == "ytdlp"
    assert plan.url == asset.source_url
    assert plan.ext == "mp4"
    assert plan.asset == asset


def test_non_video_kind_returns_empty(factory: FakeFactory) -> None:
    provider = YouTubeProvider(ydl_factory=factory)
    assert provider.search("q", Kind.IMAGE, SearchFilters(kind=Kind.IMAGE)) == []
    assert factory.instances == []


def test_load_builtins_registers_youtube_idempotently() -> None:
    registry.load_builtins()
    first = registry.get("youtube")
    assert isinstance(first, YouTubeProvider)
    registry.load_builtins()
    assert registry.get("youtube") is first
    assert [p.id for p in registry.all_providers()] == [
        "freesound",
        "google_fonts",
        "internet_archive",
        "lottiefiles",
        "mixkit",
        "openverse",
        "pexels",
        "pixabay",
        "wikimedia",
        "youtube",
    ]


def _entry(video_id: str, duration: float | None) -> dict[str, Any]:
    return {
        "_type": "url",
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": f"Video {video_id}",
        "duration": duration,
    }


def _search(
    query: str, filters: SearchFilters, entries: list[dict[str, Any]] | None = None
) -> tuple[FakeYDL, list[Any]]:
    factory = FakeFactory(result={"entries": entries if entries is not None else []})
    assets = YouTubeProvider(ydl_factory=factory).search(query, Kind.VIDEO, filters)
    return factory.instances[0], assets


SHORT_URL = "https://www.youtube.com/results?search_query={}&sp=EgQQARgB"


@pytest.mark.parametrize(
    ("style", "url", "playlistend"),
    [
        ("any", "ytsearch30:crying cat", None),
        ("meme", SHORT_URL.format("crying+cat+meme"), 30),
        ("green_screen", SHORT_URL.format("crying+cat+green+screen"), 30),
        ("cinematic", "ytsearch30:crying cat cinematic b-roll", None),
    ],
)
def test_style_search_url_and_playlistend(style: str, url: str, playlistend: int | None) -> None:
    ydl, _ = _search("crying cat", SearchFilters(kind=Kind.VIDEO, style=style))  # type: ignore[arg-type]
    assert ydl.queries == [url]
    assert ydl.opts.get("playlistend") == playlistend
    assert ydl.opts["extract_flat"] == "in_playlist"


@pytest.mark.parametrize(
    ("style", "query"),
    [
        ("meme", "Crying Cat MEME"),
        ("meme", "cat greenscreen"),
        ("meme", "cat template"),
        ("green_screen", "cat Green Screen"),
        ("green_screen", "cat chroma key"),
        ("cinematic", "rain B-Roll"),
        ("cinematic", "rain broll"),
        ("cinematic", "rain stock footage"),
        ("cinematic", "Cinematic rain"),
    ],
)
def test_augmentation_skipped_when_term_present(style: str, query: str) -> None:
    ydl, _ = _search(query, SearchFilters(kind=Kind.VIDEO, style=style))  # type: ignore[arg-type]
    assert ydl.queries[0] in (f"ytsearch30:{query}", SHORT_URL.format(quote_plus(query)))


def test_limit_sets_fetch_size_and_truncates() -> None:
    entries = [_entry(f"v{i:010d}", 10) for i in range(40)]
    ydl, assets = _search("q", SearchFilters(kind=Kind.VIDEO, style="meme", limit=8), entries)
    assert ydl.opts["playlistend"] == 24
    assert len(assets) == 8
    assert [a.id for a in assets] == [f"v{i:010d}" for i in range(8)]
    big, _ = _search("q", SearchFilters(kind=Kind.VIDEO, limit=50))
    assert big.queries == ["ytsearch60:q"]
    small, _ = _search("q", SearchFilters(kind=Kind.VIDEO, limit=2))
    assert small.queries == ["ytsearch20:q"]


def test_explicit_max_duration_overrides_style_default() -> None:
    long_cap, _ = _search("q", SearchFilters(kind=Kind.VIDEO, style="meme", max_duration=900))
    assert long_cap.queries == ["ytsearch30:q meme"]
    short_cap, _ = _search("q", SearchFilters(kind=Kind.VIDEO, max_duration=60))
    assert short_cap.queries == [SHORT_URL.format("q")]
    assert short_cap.opts["playlistend"] == 30


def test_long_form_and_missing_duration_dropped() -> None:
    entries = [
        _entry("short000001", 12),
        _entry("long0000001", 3600),
        _entry("nodur000001", None),
        _entry("edge0000001", 120),
        _entry("over0000001", 121),
    ]
    _, assets = _search("q", SearchFilters(kind=Kind.VIDEO, style="meme"), entries)
    assert [a.id for a in assets] == ["short000001", "edge0000001"]
    _, assets = _search("q", SearchFilters(kind=Kind.VIDEO), entries)
    assert [a.id for a in assets] == ["short000001", "edge0000001", "over0000001"]


def test_min_duration_respected() -> None:
    entries = [_entry("tiny0000001", 3), _entry("ok000000001", 30)]
    _, assets = _search("q", SearchFilters(kind=Kind.VIDEO, min_duration=5), entries)
    assert [a.id for a in assets] == ["ok000000001"]


def test_extra_search_query_is_augmented_query() -> None:
    _, assets = _search(
        "crying cat", SearchFilters(kind=Kind.VIDEO, style="meme"), [_entry("a0000000001", 8)]
    )
    assert assets[0].extra["search_query"] == "crying cat meme"


# --- shorts ------------------------------------------------------------------------------------

SHORTS_FILTERS = SearchFilters(kind=Kind.VIDEO, shorts=True)


def _short_entry(video_id: str, duration: float) -> dict[str, Any]:
    return {**_entry(video_id, duration), "url": f"https://www.youtube.com/shorts/{video_id}"}


@pytest.mark.parametrize(
    ("query", "style", "expected"),
    [
        ("crying cat", "any", "crying+cat+shorts"),
        ("crying cat shorts", "any", "crying+cat+shorts"),
        ("Crying Cat SHORTS", "any", "Crying+Cat+SHORTS"),
        ("crying cat", "meme", "crying+cat+meme+shorts"),
    ],
)
def test_shorts_query_term_added_once(query: str, style: str, expected: str) -> None:
    filters = SearchFilters(kind=Kind.VIDEO, style=style, shorts=True)  # type: ignore[arg-type]
    ydl, _ = _search(query, filters)
    assert ydl.queries == [SHORT_URL.format(expected)]
    assert ydl.queries[0].lower().count("shorts") == 1


def test_shorts_uses_short_url_and_drops_over_60s() -> None:
    entries = [
        _entry("ok000000030", 30),
        _entry("edge0000060", 60),
        _entry("over0000061", 61),
        _entry("long0000200", 200),
    ]
    ydl, assets = _search("q", SHORTS_FILTERS, entries)
    assert ydl.queries == [SHORT_URL.format("q+shorts")]
    assert ydl.opts["playlistend"] == 90
    assert [a.id for a in assets] == ["ok000000030", "edge0000060"]
    # an explicit max below 60 s still wins; a larger one is lowered to 60 s
    _, assets = _search("q", SHORTS_FILTERS.model_copy(update={"max_duration": 45}), entries)
    assert [a.id for a in assets] == ["ok000000030"]
    _, assets = _search("q", SHORTS_FILTERS.model_copy(update={"max_duration": 900}), entries)
    assert [a.id for a in assets] == ["ok000000030", "edge0000060"]


@pytest.mark.parametrize(
    ("limit", "plain", "shorts"),
    [(None, 30, 90), (2, 20, 60), (8, 24, 72), (50, 60, 90)],
)
def test_shorts_fetches_larger_pool(limit: int | None, plain: int, shorts: int) -> None:
    base = SearchFilters(kind=Kind.VIDEO, style="meme", limit=limit)
    without, _ = _search("q", base)
    assert without.opts["playlistend"] == plain
    with_shorts, _ = _search("q", base.model_copy(update={"shorts": True}))
    assert with_shorts.opts["playlistend"] == shorts


def test_shorts_sorts_portrait_first_stably() -> None:
    entries = [
        _entry("land0000001", 10),
        _short_entry("port0000001", 20),
        _entry("land0000002", 30),
        _short_entry("port0000002", 40),
    ]
    _, assets = _search("q", SHORTS_FILTERS, entries)
    assert [a.id for a in assets] == ["port0000001", "port0000002", "land0000001", "land0000002"]
    _, plain = _search("q", VIDEO_FILTERS, entries)
    assert [a.id for a in plain] == ["land0000001", "port0000001", "land0000002", "port0000002"]


def test_shorts_ignored_for_audio() -> None:
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, shorts=True)
    ydl, assets = _audio_search("lofi", Kind.AUDIO_MUSIC, filters)
    assert ydl.queries == ["ytsearch30:lofi"]
    assert [a.id for a in assets] == [
        "sfx00000001",
        "edge0000060",
        "mid00000061",
        "song0000900",
    ]


# --- audio kinds -------------------------------------------------------------------------------

AUDIO_ENTRIES = [
    _entry("sfx00000001", 5),
    _entry("edge0000060", 60),
    _entry("mid00000061", 61),
    _entry("song0000900", 900),
    _entry("long0000901", 901),
]


def _audio_search(
    query: str, kind: Kind, filters: SearchFilters | None = None
) -> tuple[FakeYDL, list[Any]]:
    factory = FakeFactory(result={"entries": AUDIO_ENTRIES})
    filters = filters or SearchFilters(kind=kind, style="meme")
    assets = YouTubeProvider(ydl_factory=factory).search(query, kind, filters)
    return factory.instances[0], assets


def test_kinds_include_audio() -> None:
    assert YouTubeProvider.kinds == {Kind.VIDEO, Kind.AUDIO_MUSIC, Kind.AUDIO_SFX}


def test_audio_sfx_kind_query_and_60s_cap() -> None:
    ydl, assets = _audio_search("bonk", Kind.AUDIO_SFX)
    # No " meme" style suffix for audio, and the 60 s cap uses the short-video filter.
    assert ydl.queries == [SHORT_URL.format(quote_plus("bonk"))]
    assert [a.id for a in assets] == ["sfx00000001", "edge0000060"]
    assert all(a.kind == Kind.AUDIO_SFX for a in assets)
    assert assets[0].extra["search_query"] == "bonk"


def test_audio_music_kind_query_and_900s_cap() -> None:
    ydl, assets = _audio_search("undertale bgm", Kind.AUDIO_MUSIC)
    assert ydl.queries == ["ytsearch30:undertale bgm"]
    assert [a.id for a in assets] == ["sfx00000001", "edge0000060", "mid00000061", "song0000900"]
    assert all(a.kind == Kind.AUDIO_MUSIC for a in assets)


def test_audio_max_duration_filter_overrides_default() -> None:
    filters = SearchFilters(kind=Kind.AUDIO_SFX, max_duration=100)
    _, assets = _audio_search("bonk", Kind.AUDIO_SFX, filters)
    assert [a.id for a in assets] == ["sfx00000001", "edge0000060", "mid00000061"]


def test_resolve_audio_kind_is_mp3() -> None:
    provider = YouTubeProvider(ydl_factory=FakeFactory(result={"entries": AUDIO_ENTRIES}))
    asset = provider.search("bonk", Kind.AUDIO_SFX, SearchFilters(kind=Kind.AUDIO_SFX))[0]
    plan = provider.resolve(asset)
    assert plan.method == "ytdlp"
    assert plan.ext == "mp3"


def test_registry_audio_kinds_include_youtube() -> None:
    registry.load_builtins()
    for kind in (Kind.AUDIO_MUSIC, Kind.AUDIO_SFX):
        assert "youtube" in [p.id for p in registry.providers_for(kind)]


@pytest.mark.live
def test_live_meme_style_is_short() -> None:
    filters = SearchFilters(kind=Kind.VIDEO, style="meme", limit=8)
    assets = YouTubeProvider().search("crying cat", Kind.VIDEO, filters)
    assert len(assets) >= 1
    assert all(a.duration_s is not None and a.duration_s <= 120 for a in assets)


@pytest.mark.live
def test_live_search() -> None:
    assets = YouTubeProvider(max_results=3).search("big buck bunny", Kind.VIDEO, VIDEO_FILTERS)
    assert len(assets) >= 1
    assert all(len(a.id) == 11 for a in assets)


def test_load_builtins_reads_cookies_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_COOKIES_FROM_BROWSER", "Firefox")
    registry.load_builtins()
    provider = registry.get("youtube")
    assert isinstance(provider, YouTubeProvider)
    assert provider.cookies_from_browser == ("firefox", None, None, None)


def test_load_builtins_ignores_invalid_cookies_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_COOKIES_FROM_BROWSER", "netscape")
    registry.load_builtins()
    provider = registry.get("youtube")
    assert isinstance(provider, YouTubeProvider)
    assert provider.cookies_from_browser is None


def test_age_restricted_entries_dropped_only_when_safe() -> None:
    data = load_fixture()
    data["entries"][0]["age_limit"] = 18
    data["entries"][1]["age_limit"] = 0
    factory = FakeFactory(result=data)
    safe = YouTubeProvider(ydl_factory=factory).search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert [a.id for a in safe] == ["bbbbbbbbbbb"]
    unsafe = YouTubeProvider(ydl_factory=factory).search(
        "q", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO, safe=False)
    )
    assert [a.id for a in unsafe] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
