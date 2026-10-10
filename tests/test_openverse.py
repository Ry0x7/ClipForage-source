import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforage.models import Asset, Kind, SearchFilters
from clipforage.providers.base import Provider, ProviderError
from clipforage.providers.openverse import AUDIO_URL, IMAGES_URL, OpenverseProvider

FIXTURES = Path(__file__).parent / "fixtures" / "openverse"
TOKEN = "test-openverse-token"
IMAGE_FILTERS = SearchFilters(kind=Kind.IMAGE)
SFX_FILTERS = SearchFilters(kind=Kind.AUDIO_SFX)
MUSIC_FILTERS = SearchFilters(kind=Kind.AUDIO_MUSIC)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIPFORAGE_OPENVERSE_KEY", raising=False)


@pytest.fixture
def client() -> httpx.Client:
    return httpx.Client()


@pytest.fixture
def provider(client: httpx.Client) -> OpenverseProvider:
    return OpenverseProvider(client=client)


@respx.mock
def test_image_search_request_and_mapping(provider: OpenverseProvider) -> None:
    route = respx.get(IMAGES_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("images_search.json"))
    )
    assets = provider.search("sunset", Kind.IMAGE, IMAGE_FILTERS)

    assert route.call_count == 1
    params = route.calls.last.request.url.params
    assert params["q"] == "sunset"
    assert params["page_size"] == "20"
    assert "aspect_ratio" not in params
    assert "license_type" not in params
    assert "category" not in params
    assert "authorization" not in route.calls.last.request.headers

    assert len(assets) == 2
    wide, tall = assets
    for asset in assets:
        assert asset.provider == "openverse"
        assert asset.kind == Kind.IMAGE
        assert asset.duration_s is None
    assert wide.id == "f9384235-b72e-4f1e-9b05-e1b116262a29"
    assert wide.title == "Sunset over the bay"
    assert wide.source_url == "https://www.flickr.com/photos/example/1234567890"
    assert wide.preview_url == (
        "https://api.openverse.org/v1/images/f9384235-b72e-4f1e-9b05-e1b116262a29/thumb/"
    )
    assert wide.media_url is None and wide.media_type is None
    assert wide.license == "CC BY 4.0"
    assert wide.commercial_ok is True
    assert wide.attribution_text.startswith('"Sunset over the bay" by Jane Doe is licensed')
    assert wide.orientation == "landscape"
    assert wide.extra == {
        "download_url": "https://live.staticflickr.com/65535/1234567890_abcdef_b.jpg",
        "filetype": "jpg",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "creator": "Jane Doe",
        "creator_url": "https://www.flickr.com/photos/example",
        "provider": "flickr",
    }

    assert tall.license == "CC BY-NC-SA 2.0"
    assert tall.commercial_ok is False
    assert tall.orientation == "portrait"
    # API attribution is null -> fallback string
    assert tall.attribution_text == "Old lighthouse by John Roe (CC BY-NC-SA 2.0)"


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [("landscape", "wide"), ("portrait", "tall"), ("square", "square")],
)
@respx.mock
def test_image_orientation_maps_to_aspect_ratio(
    provider: OpenverseProvider, orientation: str, expected: str
) -> None:
    route = respx.get(IMAGES_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    filters = SearchFilters(kind=Kind.IMAGE, orientation=orientation)  # type: ignore[arg-type]
    assert provider.search("x", Kind.IMAGE, filters) == []
    assert route.calls.last.request.url.params["aspect_ratio"] == expected


@respx.mock
def test_commercial_only_sends_license_type(provider: OpenverseProvider) -> None:
    route = respx.get(IMAGES_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    provider.search("x", Kind.IMAGE, SearchFilters(kind=Kind.IMAGE, commercial_only=True))
    assert route.calls.last.request.url.params["license_type"] == "commercial"


@respx.mock
def test_sfx_search_request_and_mapping(provider: OpenverseProvider) -> None:
    route = respx.get(AUDIO_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("audio_search.json"))
    )
    assets = provider.search("door", Kind.AUDIO_SFX, SFX_FILTERS)

    params = route.calls.last.request.url.params
    assert params["q"] == "door"
    assert params["category"] == "sound_effect"
    assert params["page_size"] == "20"
    assert "aspect_ratio" not in params
    assert "license_type" not in params

    sfx, music = assets
    assert sfx.kind == Kind.AUDIO_SFX
    assert sfx.license == "CC0 1.0"
    assert sfx.commercial_ok is True
    assert sfx.duration_s == 4.2
    assert sfx.orientation is None
    assert sfx.preview_url is None
    assert sfx.source_url == "https://freesound.org/people/example/sounds/123456/"
    assert sfx.extra["download_url"] == "https://cdn.freesound.org/previews/123/123456_789-hq.mp3"
    assert sfx.extra["provider"] == "freesound"
    assert sfx.media_url == "https://cdn.freesound.org/previews/123/123456_789-hq.mp3"
    assert sfx.media_type == "audio/mpeg"

    assert music.license == "CC BY-NC 3.0"
    assert music.commercial_ok is False
    assert music.duration_s == 185.0
    # filetype null in the fixture -> media_type unknown
    assert music.media_url == "https://prod-1.storage.jamendo.com/?trackid=1532771&format=mp31"
    assert music.media_type is None


@respx.mock
def test_music_search_sends_music_category(provider: OpenverseProvider) -> None:
    route = respx.get(AUDIO_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("audio_search.json"))
    )
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, commercial_only=True)
    assets = provider.search("synth", Kind.AUDIO_MUSIC, filters)
    params = route.calls.last.request.url.params
    assert params["category"] == "music"
    assert params["license_type"] == "commercial"
    assert all(a.kind == Kind.AUDIO_MUSIC for a in assets)


@respx.mock
def test_audio_duration_filter_is_client_side(provider: OpenverseProvider) -> None:
    route = respx.get(AUDIO_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("audio_search.json"))
    )
    short = provider.search(
        "q", Kind.AUDIO_SFX, SearchFilters(kind=Kind.AUDIO_SFX, max_duration=10)
    )
    assert [a.duration_s for a in short] == [4.2]
    long = provider.search("q", Kind.AUDIO_SFX, SearchFilters(kind=Kind.AUDIO_SFX, min_duration=60))
    assert [a.duration_s for a in long] == [185.0]
    params = route.calls.last.request.url.params
    assert "min_duration" not in params and "duration" not in params


@respx.mock
def test_pdm_license_and_skips_hits_missing_urls(provider: OpenverseProvider) -> None:
    base = {"title": "t", "license": "pdm", "license_version": "1.0", "creator": "c"}
    payload = {
        "results": [
            {
                **base,
                "id": "ok",
                "url": "https://x.test/a.jpg",
                "foreign_landing_url": "https://x.test/a",
            },
            {**base, "id": "no-url", "foreign_landing_url": "https://x.test/b"},
            {**base, "id": "no-landing", "url": "https://x.test/c.jpg"},
            {**base, "id": "empty-url", "url": "", "foreign_landing_url": "https://x.test/d"},
        ]
    }
    respx.get(IMAGES_URL).mock(return_value=httpx.Response(200, json=payload))
    assets = provider.search("q", Kind.IMAGE, IMAGE_FILTERS)
    assert [a.id for a in assets] == ["ok"]
    assert assets[0].license == "Public Domain Mark"
    assert assets[0].commercial_ok is True


@respx.mock
def test_resolve_returns_http_plans(provider: OpenverseProvider) -> None:
    respx.get(IMAGES_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("images_search.json"))
    )
    respx.get(AUDIO_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("audio_search.json"))
    )
    jpg, png = provider.search("q", Kind.IMAGE, IMAGE_FILTERS)
    plan = provider.resolve(jpg)
    assert plan.method == "http"
    assert plan.url == jpg.extra["download_url"]
    assert plan.ext == "jpg"
    assert plan.asset == jpg
    assert provider.resolve(png).ext == "png"

    mp3, no_filetype = provider.search("q", Kind.AUDIO_MUSIC, MUSIC_FILTERS)
    assert provider.resolve(mp3).ext == "mp3"
    # filetype null and URL has no suffix -> mp3 default for audio
    assert provider.resolve(no_filetype).ext == "mp3"


def test_resolve_ext_falls_back_to_url_suffix_then_jpg(provider: OpenverseProvider) -> None:
    def image(url: str) -> Asset:
        return Asset(
            provider="openverse",
            id="1",
            kind=Kind.IMAGE,
            title="t",
            source_url="https://x.test/",
            license="CC BY 4.0",
            attribution_text="a",
            commercial_ok=True,
            extra={"download_url": url, "filetype": None},
        )

    assert provider.resolve(image("https://x.test/pic.webp")).ext == "webp"
    assert provider.resolve(image("https://x.test/pic")).ext == "jpg"


@respx.mock
def test_unsupported_kind_returns_empty_without_http(provider: OpenverseProvider) -> None:
    route = respx.get(url__startswith="https://api.openverse.org/")
    assert provider.search("q", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO)) == []
    assert route.call_count == 0


@pytest.mark.parametrize("status", [429, 500])
@respx.mock
def test_http_error_raises_provider_error(provider: OpenverseProvider, status: int) -> None:
    respx.get(IMAGES_URL).mock(return_value=httpx.Response(status, text="Request was throttled"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.IMAGE, IMAGE_FILTERS)
    assert info.value.provider == "openverse"
    assert str(status) in info.value.message


@respx.mock
def test_bad_json_raises_provider_error(provider: OpenverseProvider) -> None:
    respx.get(AUDIO_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.AUDIO_SFX, SFX_FILTERS)
    assert info.value.provider == "openverse"


@respx.mock
def test_transport_error_raises_provider_error_without_token(client: httpx.Client) -> None:
    respx.get(IMAGES_URL).mock(side_effect=httpx.ConnectError(f"failed Bearer {TOKEN}"))
    with pytest.raises(ProviderError) as info:
        OpenverseProvider(client=client, api_key=TOKEN).search("q", Kind.IMAGE, IMAGE_FILTERS)
    assert "ConnectError" in info.value.message
    assert TOKEN not in str(info.value)


@respx.mock
def test_bearer_header_sent_when_key_given(client: httpx.Client) -> None:
    route = respx.get(IMAGES_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    OpenverseProvider(client=client, api_key=TOKEN).search("q", Kind.IMAGE, IMAGE_FILTERS)
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


@respx.mock
def test_bearer_header_from_env(monkeypatch: pytest.MonkeyPatch, client: httpx.Client) -> None:
    monkeypatch.setenv("CLIPFORAGE_OPENVERSE_KEY", "env-token")
    route = respx.get(AUDIO_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    OpenverseProvider(client=client).search("q", Kind.AUDIO_SFX, SFX_FILTERS)
    assert route.calls.last.request.headers["authorization"] == "Bearer env-token"


@respx.mock
def test_page_size_clamped_to_20_without_token(client: httpx.Client) -> None:
    route = respx.get(IMAGES_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    OpenverseProvider(client=client, page_size=100).search("q", Kind.IMAGE, IMAGE_FILTERS)
    assert route.calls.last.request.url.params["page_size"] == "20"


@respx.mock
def test_page_size_up_to_500_with_token(client: httpx.Client) -> None:
    route = respx.get(IMAGES_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    OpenverseProvider(client=client, page_size=100, api_key=TOKEN).search(
        "q", Kind.IMAGE, IMAGE_FILTERS
    )
    assert route.calls.last.request.url.params["page_size"] == "100"
    OpenverseProvider(client=client, page_size=900, api_key=TOKEN).search(
        "q", Kind.IMAGE, IMAGE_FILTERS
    )
    assert route.calls.last.request.url.params["page_size"] == "500"


@respx.mock
def test_page_size_cap_follows_env_token(
    monkeypatch: pytest.MonkeyPatch, client: httpx.Client
) -> None:
    monkeypatch.setenv("CLIPFORAGE_OPENVERSE_KEY", "env-token")
    route = respx.get(AUDIO_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    OpenverseProvider(client=client, page_size=50).search("q", Kind.AUDIO_SFX, SFX_FILTERS)
    assert route.calls.last.request.url.params["page_size"] == "50"


def test_class_attributes() -> None:
    assert OpenverseProvider.id == "openverse"
    assert OpenverseProvider.kinds == {Kind.IMAGE, Kind.AUDIO_SFX, Kind.AUDIO_MUSIC}
    assert OpenverseProvider.requires_key is False
    assert OpenverseProvider.SEARCH_TTL_S == 3600


def test_satisfies_protocol() -> None:
    assert isinstance(OpenverseProvider(), Provider)


@respx.mock
def test_safe_sends_mature_false_and_drops_mature_hits(provider: OpenverseProvider) -> None:
    data = load_fixture("images_search.json")
    data["results"][0]["mature"] = True
    route = respx.get(IMAGES_URL).mock(return_value=httpx.Response(200, json=data))
    safe = provider.search("cat", Kind.IMAGE, IMAGE_FILTERS)
    assert route.calls.last.request.url.params["mature"] == "false"
    assert [a.id for a in safe] == [data["results"][1]["id"]]
    unsafe = provider.search("cat", Kind.IMAGE, SearchFilters(kind=Kind.IMAGE, safe=False))
    assert "mature" not in route.calls.last.request.url.params
    assert len(unsafe) == 2


@respx.mock
def test_cleared_only_restricts_licenses(provider: OpenverseProvider) -> None:
    route = respx.get(AUDIO_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("audio_music_cleared.json"))
    )
    filters = SearchFilters(kind=Kind.AUDIO_MUSIC, cleared_only=True)
    assets = provider.search("lofi", Kind.AUDIO_MUSIC, filters)

    params = route.calls.last.request.url.params
    assert params["category"] == "music"
    assert params["license"] == "cc0,pdm,by,by-sa"
    assert params["license_type"] == "commercial"
    assert params["page_size"] == "20"
    assert [a.license for a in assets] == ["CC BY-SA 3.0", "CC BY-SA 3.0", "CC BY 3.0"]
    assert all(a.commercial_ok is True for a in assets)


@respx.mock
def test_no_license_param_without_cleared_only(provider: OpenverseProvider) -> None:
    route = respx.get(AUDIO_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    provider.search("lofi", Kind.AUDIO_MUSIC, MUSIC_FILTERS)
    params = route.calls.last.request.url.params
    assert "license" not in params
    assert "license_type" not in params
