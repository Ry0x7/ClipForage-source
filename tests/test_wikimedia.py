import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforage.models import Kind, SearchFilters
from clipforage.providers.base import Provider, ProviderError
from clipforage.providers.wikimedia import API_URL, WikimediaProvider

FIXTURES = Path(__file__).parent / "fixtures" / "wikimedia"
VIDEO_FILTERS = SearchFilters(kind=Kind.VIDEO)
IMAGE_FILTERS = SearchFilters(kind=Kind.IMAGE)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def provider(sleeps: list[float]) -> WikimediaProvider:
    return WikimediaProvider(client=httpx.Client(), sleep=sleeps.append)


@respx.mock
def test_video_search_request_params_and_user_agent(provider: WikimediaProvider) -> None:
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("search_video.json"))
    )
    provider.search("rain", Kind.VIDEO, VIDEO_FILTERS)

    assert route.call_count == 1
    request = route.calls.last.request
    params = request.url.params
    assert "rain" in params["gsrsearch"]
    assert "filetype:video" in params["gsrsearch"]
    assert params["gsrnamespace"] == "6"
    assert params["formatversion"] == "2"
    assert params["action"] == "query"
    assert params["generator"] == "search"
    assert params["prop"] == "imageinfo"
    assert params["iiprop"] == "url|extmetadata|mime|size"
    assert params["iiurlwidth"] == "320"
    assert params["maxlag"] == "5"
    assert params["gsrlimit"] == "20"
    ua = request.headers["User-Agent"]
    assert "clipforage" in ua
    assert "github.com" in ua
    assert "github.com/Ry0x7/ClipForage-source/issues" in ua


@respx.mock
def test_video_mapping(provider: WikimediaProvider) -> None:
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("search_video.json"))
    )
    webm, ogv = provider.search("rain", Kind.VIDEO, VIDEO_FILTERS)

    assert webm.provider == "wikimedia"
    assert webm.id == "107365060"
    assert webm.kind == Kind.VIDEO
    assert webm.title == "Rain falling on road.webm"
    assert webm.source_url == "https://commons.wikimedia.org/wiki/File:Rain_falling_on_road.webm"
    assert webm.preview_url is not None and webm.preview_url.endswith(".jpg")
    assert webm.license == "CC BY-SA 4.0"
    assert webm.commercial_ok is True
    assert webm.attribution_text == "Jane Example / Wikimedia Commons, CC BY-SA 4.0"
    assert "<" not in webm.attribution_text
    assert webm.duration_s == 12.5
    assert webm.orientation == "landscape"
    assert webm.extra["mime"] == "video/webm"
    assert webm.extra["license_url"] == "https://creativecommons.org/licenses/by-sa/4.0"
    assert webm.extra["attribution_required"] is True
    assert webm.extra["download_url"].startswith(
        "https://upload.wikimedia.org/wikipedia/commons/e/eb/Rain_falling_on_road.webm"
    )

    # no Artist -> Credit is used; HTML stripped
    assert ogv.license == "Public domain"
    assert ogv.commercial_ok is True
    assert ogv.attribution_text == "Internet Archive / Wikimedia Commons, Public domain"
    assert ogv.orientation == "portrait"
    assert ogv.duration_s == 30.0
    assert ogv.extra["license_url"] is None
    assert ogv.extra["attribution_required"] is False

    assert webm.media_url == (
        "https://upload.wikimedia.org/wikipedia/commons/e/eb/Rain_falling_on_road.webm"
        "?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=original"
    )
    assert webm.media_type == "video/webm"
    assert (
        ogv.media_url == "https://upload.wikimedia.org/wikipedia/commons/a/ab/Old_newsreel_clip.ogv"
    )
    assert ogv.media_type == "video/ogg"


@respx.mock
def test_image_search_drops_svg_and_maps(provider: WikimediaProvider) -> None:
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("search_image.json"))
    )
    assets = provider.search("cat", Kind.IMAGE, IMAGE_FILTERS)

    gsrsearch = route.calls.last.request.url.params["gsrsearch"]
    assert "cat" in gsrsearch and "filetype:bitmap" in gsrsearch
    assert [a.id for a in assets] == ["2200001"]
    jpg = assets[0]
    assert jpg.kind == Kind.IMAGE
    assert jpg.license == "CC BY 2.0"
    assert jpg.commercial_ok is True
    assert jpg.attribution_text == "John & Sons from Somewhere / Wikimedia Commons, CC BY 2.0"
    assert jpg.duration_s is None
    assert jpg.orientation == "landscape"
    assert jpg.media_url is None and jpg.media_type is None
    assert provider.resolve(jpg).ext == "jpg"


@respx.mock
def test_gif_kind_keeps_only_gif_mime(provider: WikimediaProvider) -> None:
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("search_image.json"))
    )
    assert provider.search("cat", Kind.GIF, SearchFilters(kind=Kind.GIF)) == []
    gsrsearch = route.calls.last.request.url.params["gsrsearch"]
    assert gsrsearch == "cat filemime:image/gif"
    assert "filetype:bitmap" not in gsrsearch


@pytest.mark.parametrize(
    ("license_short", "expected"),
    [
        ("CC BY-NC-SA 3.0", False),
        ("Fair use", False),
        ("CC0", True),
        (None, None),
    ],
)
@respx.mock
def test_commercial_ok_mapping(
    provider: WikimediaProvider, license_short: str | None, expected: bool | None
) -> None:
    data = load_fixture("search_image.json")
    meta = data["query"]["pages"][0]["imageinfo"][0]["extmetadata"]
    if license_short is None:
        del meta["LicenseShortName"]
    else:
        meta["LicenseShortName"]["value"] = license_short
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=data))
    (asset,) = provider.search("cat", Kind.IMAGE, IMAGE_FILTERS)
    assert asset.commercial_ok is expected
    assert asset.license == (license_short or "unknown")


@respx.mock
def test_audio_search_uses_audio_filetype(provider: WikimediaProvider) -> None:
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, json={"batchcomplete": True}))
    assert provider.search("bird", Kind.AUDIO_SFX, SearchFilters(kind=Kind.AUDIO_SFX)) == []
    assert "filetype:audio" in route.calls.last.request.url.params["gsrsearch"]


@respx.mock
def test_pages_without_imageinfo_are_skipped(provider: WikimediaProvider) -> None:
    data = {"query": {"pages": [{"pageid": 1, "ns": 6, "title": "File:X.webm", "missing": True}]}}
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=data))
    assert provider.search("x", Kind.VIDEO, VIDEO_FILTERS) == []


@respx.mock
def test_resolve_webm(provider: WikimediaProvider) -> None:
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("search_video.json"))
    )
    webm, ogv = provider.search("rain", Kind.VIDEO, VIDEO_FILTERS)
    plan = provider.resolve(webm)
    assert plan.method == "http"
    assert plan.ext == "webm"
    assert plan.url == webm.extra["download_url"]
    assert plan.asset == webm
    assert provider.resolve(ogv).ext == "ogv"


@respx.mock
def test_unsupported_kind_returns_empty_without_http(provider: WikimediaProvider) -> None:
    route = respx.get(url__startswith="https://commons.wikimedia.org/")
    assert provider.search("q", Kind.FONT, SearchFilters(kind=Kind.FONT)) == []
    assert route.call_count == 0


@respx.mock
def test_api_error_raises_provider_error(provider: WikimediaProvider, sleeps: list[float]) -> None:
    error = {"error": {"code": "badvalue", "info": "Unrecognized value for parameter"}}
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=error))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert info.value.provider == "wikimedia"
    assert "badvalue" in info.value.message
    assert sleeps == []


@respx.mock
def test_maxlag_retry_succeeds_on_second_response(
    provider: WikimediaProvider, sleeps: list[float]
) -> None:
    maxlag = {"error": {"code": "maxlag", "info": "Waiting for a database server: 6 seconds"}}
    route = respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(200, json=maxlag),
            httpx.Response(200, json=load_fixture("search_video.json")),
        ]
    )
    assets = provider.search("rain", Kind.VIDEO, VIDEO_FILTERS)
    assert route.call_count == 2
    assert sleeps == [1.0]
    assert len(assets) == 2


@respx.mock
def test_maxlag_twice_raises_provider_error(
    provider: WikimediaProvider, sleeps: list[float]
) -> None:
    maxlag = {"error": {"code": "maxlag", "info": "lagged"}}
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, json=maxlag))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert route.call_count == 2
    assert sleeps == [1.0]
    assert "maxlag" in info.value.message


@pytest.mark.parametrize("status", [500, 503])
@respx.mock
def test_server_error_raises_provider_error(provider: WikimediaProvider, status: int) -> None:
    respx.get(API_URL).mock(return_value=httpx.Response(status, text="Service Unavailable"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.VIDEO, VIDEO_FILTERS)
    assert info.value.provider == "wikimedia"
    assert str(status) in info.value.message


@respx.mock
def test_bad_json_and_transport_errors_raise_provider_error(
    provider: WikimediaProvider,
) -> None:
    respx.get(API_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(ProviderError):
        provider.search("q", Kind.IMAGE, IMAGE_FILTERS)
    respx.get(API_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.IMAGE, IMAGE_FILTERS)
    assert "ConnectError" in info.value.message


def test_satisfies_protocol() -> None:
    assert isinstance(WikimediaProvider(), Provider)
    assert WikimediaProvider.requires_key is False
    assert WikimediaProvider.SEARCH_TTL_S == 3600
