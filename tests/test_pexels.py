import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforage.models import Asset, Kind, SearchFilters
from clipforage.providers.base import Provider, ProviderError
from clipforage.providers.pexels import PHOTOS_URL, VIDEOS_URL, PexelsProvider

FIXTURES = Path(__file__).parent / "fixtures" / "pexels"
KEY = "test-pexels-key"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def provider() -> PexelsProvider:
    return PexelsProvider(api_key=KEY, client=httpx.Client())


def assert_license_fields(asset: Asset) -> None:
    assert asset.license == "Pexels License"
    assert asset.attribution_text
    assert asset.source_url.startswith("https://www.pexels.com/")
    assert asset.commercial_ok is True
    assert Asset.model_validate(asset.model_dump()) == asset


def test_satisfies_protocol(provider: PexelsProvider) -> None:
    assert isinstance(provider, Provider)
    assert provider.id == "pexels"
    assert provider.kinds == frozenset({Kind.VIDEO, Kind.IMAGE})
    assert provider.requires_key is True


@respx.mock
def test_video_search(provider: PexelsProvider) -> None:
    route = respx.get(VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=load("videos_search.json"))
    )
    filters = SearchFilters(kind=Kind.VIDEO, orientation="landscape")
    assets = provider.search("green screen", Kind.VIDEO, filters)

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["Authorization"] == KEY
    assert request.url.params["query"] == "green screen"
    assert request.url.params["orientation"] == "landscape"
    assert request.url.params["per_page"] == "15"

    assert len(assets) == 2
    first, second = assets
    for asset in assets:
        assert_license_fields(asset)
        assert asset.provider == "pexels"
        assert asset.kind == Kind.VIDEO
    assert first.id == "3571264"
    assert first.title == "Pexels video 3571264 by Joey Farina"
    assert first.attribution_text == "Video by Joey Farina on Pexels (https://www.pexels.com/@joey)"
    assert first.source_url == "https://www.pexels.com/video/drone-view-of-a-green-field-3571264/"
    assert first.preview_url is not None and "free-video-3571264.jpg" in first.preview_url
    assert first.duration_s == 33.0
    assert first.orientation == "landscape"
    assert first.extra["width"] == 3840
    assert first.extra["height"] == 2160
    # uhd 3840 is skipped (> 1920); hd 1920 wins over sd 640; hls (width null) ignored.
    assert first.extra["download_url"].endswith("3571264-hd_1920_1080_30fps.mp4")
    assert second.orientation == "portrait"
    assert second.duration_s == 12.0
    assert second.extra["download_url"].endswith("5752729-hd_1080_1920_25fps.mp4")
    # playable preview: smallest-width "sd" mp4
    assert first.media_url == (
        "https://videos.pexels.com/video-files/3571264/3571264-sd_640_360_30fps.mp4"
    )
    assert first.media_type == "video/mp4"
    assert second.media_url == (
        "https://videos.pexels.com/video-files/5752729/5752729-sd_540_960_25fps.mp4"
    )
    assert second.media_type == "video/mp4"


@respx.mock
def test_video_falls_back_to_largest_mp4_when_all_exceed_cap(provider: PexelsProvider) -> None:
    data = load("videos_search.json")
    data["videos"] = data["videos"][:1]
    data["videos"][0]["video_files"] = [
        f for f in data["videos"][0]["video_files"] if f["quality"] in ("uhd", "hls")
    ]
    respx.get(VIDEOS_URL).mock(return_value=httpx.Response(200, json=data))
    [asset] = provider.search("x", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))
    assert asset.extra["download_url"].endswith("uhd_3840_2160_30fps.mp4")


@respx.mock
def test_photo_search(provider: PexelsProvider) -> None:
    route = respx.get(PHOTOS_URL).mock(
        return_value=httpx.Response(200, json=load("photos_search.json"))
    )
    assets = provider.search("rocks", Kind.IMAGE, SearchFilters(kind=Kind.IMAGE))

    request = route.calls.last.request
    assert request.headers["Authorization"] == KEY
    assert "orientation" not in request.url.params
    assert len(assets) == 2
    first, second = assets
    for asset in assets:
        assert_license_fields(asset)
        assert asset.kind == Kind.IMAGE
        assert asset.duration_s is None
    assert first.title == "Brown Rocks During Golden Hour"
    assert first.attribution_text == "Photo by Joey Farina on Pexels (https://www.pexels.com/@joey)"
    assert first.orientation == "square"
    assert first.preview_url is not None and first.preview_url.endswith("h=350")
    assert first.extra["download_url"].endswith("pexels-photo-2014422.jpeg")
    assert second.title == "Pexels photo 1181244 by Christina Morillo"
    assert second.orientation == "portrait"


@respx.mock
def test_video_without_mp4_is_skipped(provider: PexelsProvider) -> None:
    data = load("videos_search.json")
    data["videos"][0]["video_files"] = [{"file_type": "video/webm", "link": "https://x/a.webm"}]
    respx.get(VIDEOS_URL).mock(return_value=httpx.Response(200, json=data))
    assets = provider.search("rain", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))
    assert [a.id for a in assets] == [str(data["videos"][1]["id"])]


@respx.mock
def test_photo_without_original_is_skipped(provider: PexelsProvider) -> None:
    data = load("photos_search.json")
    del data["photos"][0]["src"]["original"]
    respx.get(PHOTOS_URL).mock(return_value=httpx.Response(200, json=data))
    assets = provider.search("rocks", Kind.IMAGE, SearchFilters(kind=Kind.IMAGE))
    assert [a.id for a in assets] == [str(data["photos"][1]["id"])]


@respx.mock
def test_unsupported_kind_makes_no_request(provider: PexelsProvider) -> None:
    route = respx.route().mock(return_value=httpx.Response(200, json={}))
    assert provider.search("x", Kind.GIF, SearchFilters(kind=Kind.GIF)) == []
    assert route.call_count == 0


@pytest.mark.parametrize("status", [401, 500])
@respx.mock
def test_http_error_raises_provider_error(provider: PexelsProvider, status: int) -> None:
    respx.get(VIDEOS_URL).mock(return_value=httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(ProviderError) as excinfo:
        provider.search("x", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))
    assert excinfo.value.provider == "pexels"
    assert str(status) in excinfo.value.message


@respx.mock
def test_transport_error_raises_provider_error(provider: PexelsProvider) -> None:
    respx.get(PHOTOS_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError, match="ConnectError"):
        provider.search("x", Kind.IMAGE, SearchFilters(kind=Kind.IMAGE))


@respx.mock
def test_bad_json_raises_provider_error(provider: PexelsProvider) -> None:
    respx.get(PHOTOS_URL).mock(return_value=httpx.Response(200, text="<html>not json"))
    with pytest.raises(ProviderError):
        provider.search("x", Kind.IMAGE, SearchFilters(kind=Kind.IMAGE))


@respx.mock
def test_missing_key_raises_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIPFORAGE_PEXELS_KEY", raising=False)
    route = respx.route().mock(return_value=httpx.Response(200, json={}))
    provider = PexelsProvider(client=httpx.Client())
    with pytest.raises(ProviderError, match="API key"):
        provider.search("x", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))
    assert route.call_count == 0


@respx.mock
def test_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_PEXELS_KEY", "from-env")
    route = respx.get(VIDEOS_URL).mock(return_value=httpx.Response(200, json={"videos": []}))
    PexelsProvider(client=httpx.Client()).search("x", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))
    assert route.calls.last.request.headers["Authorization"] == "from-env"


@respx.mock
def test_key_is_read_at_search_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIPFORAGE_PEXELS_KEY", raising=False)
    provider = PexelsProvider(client=httpx.Client())  # constructed before any key exists
    route = respx.get(VIDEOS_URL).mock(return_value=httpx.Response(200, json={"videos": []}))
    monkeypatch.setenv("CLIPFORAGE_PEXELS_KEY", "set-later")
    provider.search("x", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))
    assert route.calls.last.request.headers["Authorization"] == "set-later"
    monkeypatch.setenv("CLIPFORAGE_PEXELS_KEY", "rotated")
    provider.search("x", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))
    assert route.calls.last.request.headers["Authorization"] == "rotated"


@respx.mock
def test_resolve_video_and_photo(provider: PexelsProvider) -> None:
    respx.get(VIDEOS_URL).mock(return_value=httpx.Response(200, json=load("videos_search.json")))
    respx.get(PHOTOS_URL).mock(return_value=httpx.Response(200, json=load("photos_search.json")))
    video = provider.search("x", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))[0]
    jpeg, png = provider.search("x", Kind.IMAGE, SearchFilters(kind=Kind.IMAGE))

    plan = provider.resolve(video)
    assert plan.method == "http"
    assert plan.url == video.extra["download_url"]
    assert plan.ext == "mp4"
    assert plan.headers == {}
    assert plan.asset == video

    assert provider.resolve(jpeg).ext == "jpeg"
    assert provider.resolve(jpeg).url.endswith("pexels-photo-2014422.jpeg")
    assert provider.resolve(png).ext == "png"


def test_resolve_image_without_extension_defaults_to_jpg(provider: PexelsProvider) -> None:
    asset = Asset(
        provider="pexels",
        id="1",
        kind=Kind.IMAGE,
        title="t",
        source_url="https://www.pexels.com/photo/1/",
        license="Pexels License",
        attribution_text="Photo by x on Pexels",
        commercial_ok=True,
        extra={"download_url": "https://images.pexels.com/photos/1/original"},
    )
    assert provider.resolve(asset).ext == "jpg"


def test_resolve_without_download_url_raises_provider_error(provider: PexelsProvider) -> None:
    asset = Asset(
        provider="pexels",
        id="1",
        kind=Kind.VIDEO,
        title="t",
        source_url="https://www.pexels.com/video/1/",
        license="Pexels License",
        attribution_text="Video by x on Pexels",
        commercial_ok=True,
    )
    with pytest.raises(ProviderError):
        provider.resolve(asset)


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("CLIPFORAGE_PEXELS_KEY"), reason="no Pexels key")
def test_live_video_search() -> None:
    provider = PexelsProvider(api_key=os.environ["CLIPFORAGE_PEXELS_KEY"], per_page=3)
    assets = provider.search("green screen", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO))
    assert assets
    assert all(a.extra["download_url"] for a in assets)
