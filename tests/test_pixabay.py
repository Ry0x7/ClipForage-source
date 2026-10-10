import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforage.models import Kind, SearchFilters
from clipforage.providers.base import Provider, ProviderError
from clipforage.providers.pixabay import IMAGES_URL, VIDEOS_URL, PixabayProvider

FIXTURES = Path(__file__).parent / "fixtures" / "pixabay"
KEY = "test-pixabay-key"
VIDEO_FILTERS = SearchFilters(kind=Kind.VIDEO)
IMAGE_FILTERS = SearchFilters(kind=Kind.IMAGE)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def client() -> httpx.Client:
    return httpx.Client()


@pytest.fixture
def provider(client: httpx.Client) -> PixabayProvider:
    return PixabayProvider(api_key=KEY, client=client)


@respx.mock
def test_video_search_request_and_mapping(provider: PixabayProvider) -> None:
    route = respx.get(VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("videos_search.json"))
    )
    assets = provider.search("rain window", Kind.VIDEO, VIDEO_FILTERS)

    assert route.call_count == 1
    params = route.calls.last.request.url.params
    assert params["key"] == KEY
    assert params["q"] == "rain window"
    assert params["safesearch"] == "true"
    assert params["per_page"] == "20"
    assert "orientation" not in params

    assert [a.id for a in assets] == ["125", "204567"]
    first, second = assets
    for asset in assets:
        assert asset.provider == "pixabay"
        assert asset.kind == Kind.VIDEO
        assert asset.license == "Pixabay Content License"
        assert asset.commercial_ok is True
        assert asset.source_url.startswith("https://pixabay.com/videos/id-")
    assert first.title == "Rain, Window, Drops"
    assert second.title == "Green Screen, Phone, Vertical"
    assert first.attribution_text == (
        "Video by Coverr-Free-Footage from Pixabay (https://pixabay.com/videos/id-125/)"
    )
    assert first.duration_s == 12.0
    assert first.orientation == "landscape"
    assert first.preview_url == "https://cdn.pixabay.com/video/2015/08/08/125-135736646_tiny.jpg"
    # large is picked over medium
    assert first.extra == {
        "width": 1920,
        "height": 1080,
        "download_url": "https://cdn.pixabay.com/video/2015/08/08/125-135736646_large.mp4",
        "pixabay_id": 125,
    }
    # empty large.url falls back to medium
    assert second.extra["download_url"].endswith("_medium.mp4")
    assert (second.extra["width"], second.extra["height"]) == (720, 1280)
    assert second.orientation == "portrait"
    assert second.duration_s == 7.0
    # playable preview: tiny rendition
    assert first.media_url == "https://cdn.pixabay.com/video/2015/08/08/125-135736646_tiny.mp4"
    assert first.media_type == "video/mp4"
    assert second.media_url == "https://cdn.pixabay.com/video/2024/03/02/204567-918273645_tiny.mp4"
    assert second.media_type == "video/mp4"


@respx.mock
def test_image_search_sends_orientation_and_maps(provider: PixabayProvider) -> None:
    route = respx.get(IMAGES_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("images_search.json"))
    )
    filters = SearchFilters(kind=Kind.IMAGE, orientation="landscape")
    assets = provider.search("flower", Kind.IMAGE, filters)

    params = route.calls.last.request.url.params
    assert params["orientation"] == "horizontal"
    assert params["key"] == KEY
    assert params["safesearch"] == "true"
    assert len(assets) == 2
    flower = assets[0]
    assert flower.kind == Kind.IMAGE
    assert flower.title == "Blossom, Bloom, Flower"
    assert flower.extra["pixabay_id"] == 195893
    assert flower.license == "Pixabay Content License"
    assert flower.commercial_ok is True
    assert flower.attribution_text.startswith("Image by Josch13 from Pixabay (")
    assert flower.orientation == "landscape"
    assert flower.duration_s is None
    assert flower.preview_url == (
        "https://cdn.pixabay.com/photo/2013/10/15/09/12/flower-195893_150.jpg"
    )
    assert flower.extra["download_url"] == "https://pixabay.com/get/ed6a99fd0a76647_1280.jpg"
    assert assets[1].orientation == "portrait"


@respx.mock
def test_title_falls_back_to_id_without_tags(provider: PixabayProvider) -> None:
    data = load_fixture("videos_search.json")
    data["hits"][0]["tags"] = " , "
    respx.get(VIDEOS_URL).mock(return_value=httpx.Response(200, json=data))
    assets = provider.search("rain", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))

    assert assets[0].title == "Pixabay video 125"
    assert assets[0].extra["pixabay_id"] == 125


@respx.mock
def test_image_search_portrait_maps_to_vertical(provider: PixabayProvider) -> None:
    route = respx.get(IMAGES_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("images_search.json"))
    )
    provider.search("x", Kind.IMAGE, SearchFilters(kind=Kind.IMAGE, orientation="portrait"))
    assert route.calls.last.request.url.params["orientation"] == "vertical"


@respx.mock
def test_video_orientation_filter_is_client_side(provider: PixabayProvider) -> None:
    route = respx.get(VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("videos_search.json"))
    )
    filters = SearchFilters(kind=Kind.VIDEO, orientation="landscape")
    assets = provider.search("q", Kind.VIDEO, filters)
    assert "orientation" not in route.calls.last.request.url.params
    assert [a.id for a in assets] == ["125"]


@respx.mock
def test_unsupported_kind_returns_empty_without_http(provider: PixabayProvider) -> None:
    route = respx.get(url__startswith="https://pixabay.com/")
    assert provider.search("q", Kind.GIF, SearchFilters(kind=Kind.GIF)) == []
    assert route.call_count == 0


@pytest.mark.parametrize("status", [400, 429])
@respx.mock
def test_http_error_raises_provider_error(provider: PixabayProvider, status: int) -> None:
    respx.get(VIDEOS_URL).mock(return_value=httpx.Response(status, text="API rate limit exceeded"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert info.value.provider == "pixabay"
    assert str(status) in info.value.message
    assert KEY not in str(info.value)


@respx.mock
def test_bad_json_raises_provider_error(provider: PixabayProvider) -> None:
    respx.get(IMAGES_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.IMAGE, IMAGE_FILTERS)
    assert info.value.provider == "pixabay"


@respx.mock
def test_transport_error_raises_provider_error_without_key(provider: PixabayProvider) -> None:
    respx.get(VIDEOS_URL).mock(side_effect=httpx.ConnectError(f"failed {VIDEOS_URL}?key={KEY}"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert "ConnectError" in info.value.message
    assert KEY not in str(info.value)


@respx.mock
def test_missing_key_raises_provider_error(
    monkeypatch: pytest.MonkeyPatch, client: httpx.Client
) -> None:
    monkeypatch.delenv("CLIPFORAGE_PIXABAY_KEY", raising=False)
    route = respx.get(url__startswith="https://pixabay.com/")
    with pytest.raises(ProviderError) as info:
        PixabayProvider(client=client).search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert info.value.provider == "pixabay"
    assert "CLIPFORAGE_PIXABAY_KEY" in info.value.message
    assert route.call_count == 0


@respx.mock
def test_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch, client: httpx.Client) -> None:
    monkeypatch.setenv("CLIPFORAGE_PIXABAY_KEY", "env-key")
    route = respx.get(VIDEOS_URL).mock(return_value=httpx.Response(200, json={"hits": []}))
    assert PixabayProvider(client=client).search("q", Kind.VIDEO, VIDEO_FILTERS) == []
    assert route.calls.last.request.url.params["key"] == "env-key"


@respx.mock
def test_resolve_returns_http_plans(provider: PixabayProvider) -> None:
    respx.get(VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("videos_search.json"))
    )
    respx.get(IMAGES_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("images_search.json"))
    )
    video = provider.search("q", Kind.VIDEO, VIDEO_FILTERS)[0]
    plan = provider.resolve(video)
    assert plan.method == "http"
    assert plan.url == video.extra["download_url"]
    assert plan.ext == "mp4"
    assert plan.asset == video

    jpg, png = provider.search("q", Kind.IMAGE, IMAGE_FILTERS)
    assert provider.resolve(jpg).ext == "jpg"
    assert provider.resolve(jpg).method == "http"
    assert provider.resolve(png).ext == "png"


def test_search_ttl_is_at_least_24h() -> None:
    assert PixabayProvider.SEARCH_TTL_S >= 86400


def test_satisfies_protocol() -> None:
    assert isinstance(PixabayProvider(api_key=KEY), Provider)


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("CLIPFORAGE_PIXABAY_KEY"), reason="no Pixabay key")
def test_live_search() -> None:
    assets = PixabayProvider(per_page=3).search("rain", Kind.VIDEO, VIDEO_FILTERS)
    assert len(assets) >= 1
    assert all(a.license == "Pixabay Content License" for a in assets)


@respx.mock
def test_safesearch_param_only_when_safe(provider: PixabayProvider) -> None:
    route = respx.get(VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("videos_search.json"))
    )
    provider.search("rain", Kind.VIDEO, VIDEO_FILTERS)
    assert route.calls.last.request.url.params["safesearch"] == "true"
    provider.search("rain", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO, safe=False))
    assert "safesearch" not in route.calls.last.request.url.params
