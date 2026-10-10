from pathlib import Path

import httpx
import pytest
import respx

from clipforage import scrape
from clipforage.models import Asset, Kind, SearchFilters
from clipforage.providers import registry
from clipforage.providers.base import Provider, ProviderError
from clipforage.providers.mixkit import (
    ATTRIBUTION,
    LICENSE_FREE,
    LICENSE_RESTRICTED,
    LICENSE_UNVERIFIED,
    SFX_LIST_URL,
    VIDEO_LIST_URL,
    VIDEO_MODAL_URL,
    MixkitProvider,
    parse_clock,
    parse_iso_duration,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mixkit"
VIDEO = SearchFilters(kind=Kind.VIDEO)
SFX = SearchFilters(kind=Kind.AUDIO_SFX)
ROBOTS_URL = "https://mixkit.co/robots.txt"
CAT_URL = VIDEO_LIST_URL.format(slug="cat")
WHOOSH_URL = SFX_LIST_URL.format(slug="whoosh")
MODAL_URL = "https://mixkit.co/free-sound-effects/download/1489/?context=item+grid"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class NoSleep:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _scrape_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_ENABLED", "1")


@pytest.fixture
def provider() -> MixkitProvider:
    clock = NoSleep()
    return MixkitProvider(
        client=httpx.Client(), throttle=scrape.HostThrottle(1.0, clock=clock, sleep=clock.sleep)
    )


def mock_robots() -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=fixture("robots.txt")))
    respx.get("https://assets.mixkit.co/robots.txt").mock(return_value=httpx.Response(404))


def test_satisfies_protocol() -> None:
    p = MixkitProvider()
    assert isinstance(p, Provider)
    assert p.kinds == {Kind.VIDEO, Kind.AUDIO_SFX}
    assert p.requires_key is False and p.scrapes is True
    assert p.SEARCH_TTL_S == 86400


def test_parse_helpers() -> None:
    assert parse_clock("0:04") == 4.0
    assert parse_clock("1:02:03") == 3723.0
    assert parse_clock("n/a") is None
    assert parse_iso_duration("PT0M15S") == 15.0
    assert parse_iso_duration("PT1H2M") == 3720.0
    assert parse_iso_duration("15s") is None


@respx.mock
def test_video_search_parses_list_page(provider: MixkitProvider) -> None:
    mock_robots()
    route = respx.get(CAT_URL).mock(
        return_value=httpx.Response(200, text=fixture("video_cat.html"))
    )
    assets = provider.search("Cat", Kind.VIDEO, VIDEO)
    assert route.call_count == 1  # exactly one list page, no detail pages
    assert len(assets) == 5
    first = assets[0]
    assert first.provider == "mixkit"
    assert first.id == "video-1779"
    assert first.kind == Kind.VIDEO
    assert first.title == "Pet owner playing with a cute cat"
    assert first.source_url == (
        "https://mixkit.co/free-stock-video/pet-owner-playing-with-a-cute-cat-1779/"
    )
    assert first.preview_url == "https://assets.mixkit.co/videos/1779/1779-thumb-360-0.jpg"
    assert first.media_url == "https://assets.mixkit.co/videos/1779/1779-360.mp4"
    assert first.media_type == "video/mp4"
    assert first.attribution_text == ATTRIBUTION
    assert first.extra["mixkit_id"] == "1779"
    assert first.extra["description"] == "A pet owner playing with a cute cat with a straw."
    assert first.extra["download_url"] == "https://assets.mixkit.co/videos/1779/1779-1080.mp4"
    assert first.duration_s is None


@respx.mock
def test_video_license_mapping_from_json_ld(provider: MixkitProvider) -> None:
    mock_robots()
    respx.get(CAT_URL).mock(return_value=httpx.Response(200, text=fixture("video_cat.html")))
    by_id = {a.id: a for a in provider.search("cat", Kind.VIDEO, VIDEO)}
    free = by_id["video-1779"]
    assert (free.license, free.commercial_ok, free.extra["license_tier"]) == (
        LICENSE_FREE,
        True,
        "free",
    )
    restricted = by_id["video-49058"]
    assert (restricted.license, restricted.commercial_ok) == (LICENSE_RESTRICTED, None)
    assert restricted.extra["download_url"] == (
        "https://assets.mixkit.co/videos/49058/49058-720.mp4"
    )


@respx.mock
def test_video_without_json_ld_is_unverified(provider: MixkitProvider) -> None:
    mock_robots()
    page = fixture("video_cat.html")
    start = page.index('<script type="application/ld+json"')
    end = page.index("</script>", start) + len("</script>")
    respx.get(CAT_URL).mock(return_value=httpx.Response(200, text=page[:start] + page[end:]))
    assets = provider.search("cat", Kind.VIDEO, VIDEO)
    assert len(assets) == 5
    assert {a.license for a in assets} == {LICENSE_UNVERIFIED}
    assert {a.commercial_ok for a in assets} == {None}
    assert all(a.extra["download_url"] is None for a in assets)


@respx.mock
def test_sfx_search_parses_list_page(provider: MixkitProvider) -> None:
    mock_robots()
    route = respx.get(WHOOSH_URL).mock(
        return_value=httpx.Response(200, text=fixture("sfx_whoosh.html"))
    )
    assets = provider.search("whoosh", Kind.AUDIO_SFX, SFX)
    assert route.call_count == 1
    assert [a.id for a in assets] == ["sfx-1489", "sfx-1492", "sfx-1491", "sfx-1714", "sfx-1490"]
    first = assets[0]
    assert first.title == "Air woosh"
    assert first.duration_s == 2.0
    assert first.media_url == "https://assets.mixkit.co/active_storage/sfx/1489/1489-preview.mp3"
    assert first.media_type == "audio/mpeg"
    assert first.license == LICENSE_FREE and first.commercial_ok is True
    assert first.source_url == WHOOSH_URL
    assert first.extra["modal_url"] == MODAL_URL
    assert all(a.extra["modal_url"] for a in assets)


@respx.mock
def test_404_list_page_is_no_results(provider: MixkitProvider) -> None:
    mock_robots()
    respx.get(VIDEO_LIST_URL.format(slug="zzqx")).mock(return_value=httpx.Response(404))
    assert provider.search("zzqx", Kind.VIDEO, VIDEO) == []


@respx.mock
def test_page_without_items_is_no_results(provider: MixkitProvider) -> None:
    mock_robots()
    empty = "<html><body><p>No results</p></body></html>"
    respx.get(CAT_URL).mock(return_value=httpx.Response(200, text=empty))
    respx.get(SFX_LIST_URL.format(slug="cat")).mock(return_value=httpx.Response(200, text=empty))
    assert provider.search("cat", Kind.VIDEO, VIDEO) == []
    assert provider.search("cat", Kind.AUDIO_SFX, SFX) == []


@respx.mock
def test_server_error_raises_provider_error(provider: MixkitProvider) -> None:
    mock_robots()
    respx.get(CAT_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderError) as info:
        provider.search("cat", Kind.VIDEO, VIDEO)
    assert info.value.provider == "mixkit"
    assert "HTTP 503" in info.value.message


@respx.mock
def test_robots_disallow_raises_provider_error(provider: MixkitProvider) -> None:
    respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /free-stock-video/\n")
    )
    page = respx.get(CAT_URL).mock(return_value=httpx.Response(200, text=""))
    with pytest.raises(ProviderError, match="robots.txt disallows"):
        provider.search("cat", Kind.VIDEO, VIDEO)
    assert not page.called


def test_disabled_scraping_raises(
    provider: MixkitProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_ENABLED", "0")
    with pytest.raises(ProviderError, match="scraping is disabled"):
        provider.search("cat", Kind.VIDEO, VIDEO)


def test_disabled_scraping_skips_in_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_ENABLED", "false")
    assets, errors = registry.search_all("cat", VIDEO, [MixkitProvider()])
    assert assets == []
    assert [e.provider for e in errors] == ["mixkit"]
    assert "scraping is disabled" in errors[0].message


def test_unsupported_kind_and_empty_slug(provider: MixkitProvider) -> None:
    assert provider.search("cat", Kind.FONT, SearchFilters(kind=Kind.FONT)) == []
    assert provider.search("!!!", Kind.VIDEO, VIDEO) == []


@respx.mock
def test_resolve_video_uses_json_ld_content_url(provider: MixkitProvider) -> None:
    mock_robots()
    respx.get(CAT_URL).mock(return_value=httpx.Response(200, text=fixture("video_cat.html")))
    by_id = {a.id: a for a in provider.search("cat", Kind.VIDEO, VIDEO)}
    plan = provider.resolve(by_id["video-1779"])
    assert (plan.method, plan.url, plan.ext) == (
        "http",
        "https://assets.mixkit.co/videos/1779/1779-1080.mp4",
        "mp4",
    )
    assert provider.resolve(by_id["video-49058"]).url.endswith("/49058-720.mp4")


def _without_json_ld_url(provider: MixkitProvider) -> Asset:
    respx.get(CAT_URL).mock(return_value=httpx.Response(200, text=fixture("video_cat.html")))
    asset = provider.search("cat", Kind.VIDEO, VIDEO)[0]
    return asset.model_copy(update={"extra": {**asset.extra, "download_url": None}})


@respx.mock
def test_resolve_video_reads_download_modal(provider: MixkitProvider) -> None:
    mock_robots()
    asset = _without_json_ld_url(provider)
    # recorded for item 101239, whose 1080p file lives under an opaque active_storage key
    modal = respx.get(VIDEO_MODAL_URL.format(id="1779")).mock(
        return_value=httpx.Response(200, text=fixture("video_download_modal.html"))
    )
    plan = provider.resolve(asset)
    assert modal.call_count == 1
    assert (plan.url, plan.ext) == ("https://assets.mixkit.co/ag0flm5diyxl3p59g3g1k8wudq87", "mp4")


@respx.mock
def test_resolve_video_heads_1080_then_falls_back_to_720(provider: MixkitProvider) -> None:
    mock_robots()
    asset = _without_json_ld_url(provider)
    respx.get(VIDEO_MODAL_URL.format(id="1779")).mock(return_value=httpx.Response(500))
    head = respx.head("https://assets.mixkit.co/videos/1779/1779-1080.mp4").mock(
        return_value=httpx.Response(200)
    )
    assert provider.resolve(asset).url.endswith("/1779-1080.mp4")
    head.mock(return_value=httpx.Response(403))  # what the asset bucket says for a missing file
    assert provider.resolve(asset).url == "https://assets.mixkit.co/videos/1779/1779-720.mp4"


@respx.mock
def test_resolve_sfx_reads_download_modal(provider: MixkitProvider) -> None:
    mock_robots()
    respx.get(WHOOSH_URL).mock(return_value=httpx.Response(200, text=fixture("sfx_whoosh.html")))
    modal = respx.get(MODAL_URL).mock(
        return_value=httpx.Response(200, text=fixture("sfx_download_modal.html"))
    )
    asset = provider.search("whoosh", Kind.AUDIO_SFX, SFX)[0]
    plan = provider.resolve(asset)
    assert modal.call_count == 1
    assert (plan.method, plan.url, plan.ext) == (
        "http",
        "https://assets.mixkit.co/active_storage/sfx/1489/1489.wav",
        "wav",
    )
    assert "download_fallback" not in plan.asset.extra


@respx.mock
def test_resolve_sfx_falls_back_to_preview_and_flags_it(provider: MixkitProvider) -> None:
    mock_robots()
    respx.get(WHOOSH_URL).mock(return_value=httpx.Response(200, text=fixture("sfx_whoosh.html")))
    respx.get(MODAL_URL).mock(return_value=httpx.Response(200, text="<div>changed</div>"))
    asset = provider.search("whoosh", Kind.AUDIO_SFX, SFX)[0]
    plan = provider.resolve(asset)
    assert plan.url == asset.media_url and plan.ext == "mp3"
    assert "download modal had no file URL" in plan.asset.extra["download_fallback"]


def test_registered_by_load_builtins() -> None:
    registry.load_builtins()
    assert isinstance(registry.get("mixkit"), MixkitProvider)
    assert registry.get("mixkit") in registry.providers_for(Kind.AUDIO_SFX)
