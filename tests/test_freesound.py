import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforage.models import Kind, SearchFilters
from clipforage.providers.base import Provider, ProviderError
from clipforage.providers.freesound import FIELDS, SEARCH_URL, FreesoundProvider, _license_label

FIXTURES = Path(__file__).parent / "fixtures" / "freesound"
KEY = "test-freesound-key"
SFX_FILTERS = SearchFilters(kind=Kind.AUDIO_SFX)
DEFAULT_LICENSE_FILTER = 'license:("Creative Commons 0" OR "Attribution")'


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def client() -> httpx.Client:
    return httpx.Client()


@pytest.fixture
def provider(client: httpx.Client) -> FreesoundProvider:
    return FreesoundProvider(api_key=KEY, client=client)


def mock_search() -> respx.Route:
    return respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("search_text.json"))
    )


@respx.mock
def test_auth_header_and_key_not_in_url(provider: FreesoundProvider) -> None:
    route = mock_search()
    provider.search("door slam", Kind.AUDIO_SFX, SFX_FILTERS)
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Token {KEY}"
    assert KEY not in str(request.url)
    assert "token" not in request.url.params


@respx.mock
def test_default_params(provider: FreesoundProvider) -> None:
    route = mock_search()
    provider.search("door slam", Kind.AUDIO_SFX, SFX_FILTERS)
    params = route.calls.last.request.url.params
    assert params["query"] == "door slam"
    assert params["page_size"] == "20"
    assert params["sort"] == "score"
    assert params["fields"] == FIELDS
    assert params["filter"] == DEFAULT_LICENSE_FILTER


@respx.mock
def test_include_nc_adds_noncommercial(client: httpx.Client) -> None:
    route = mock_search()
    FreesoundProvider(api_key=KEY, client=client, include_nc=True).search(
        "q", Kind.AUDIO_SFX, SFX_FILTERS
    )
    assert route.calls.last.request.url.params["filter"] == (
        'license:("Creative Commons 0" OR "Attribution" OR "Attribution NonCommercial")'
    )


@respx.mock
def test_commercial_only_forces_commercial_licenses(client: httpx.Client) -> None:
    route = mock_search()
    filters = SearchFilters(kind=Kind.AUDIO_SFX, commercial_only=True)
    FreesoundProvider(api_key=KEY, client=client, include_nc=True).search(
        "q", Kind.AUDIO_SFX, filters
    )
    assert route.calls.last.request.url.params["filter"] == DEFAULT_LICENSE_FILTER


@respx.mock
def test_duration_range_filter(provider: FreesoundProvider) -> None:
    route = mock_search()
    filters = SearchFilters(kind=Kind.AUDIO_SFX, min_duration=1, max_duration=10.5)
    provider.search("q", Kind.AUDIO_SFX, filters)
    assert route.calls.last.request.url.params["filter"] == (
        f"{DEFAULT_LICENSE_FILTER} duration:[1 TO 10.5]"
    )
    provider.search("q", Kind.AUDIO_SFX, SearchFilters(kind=Kind.AUDIO_SFX, max_duration=5))
    assert route.calls.last.request.url.params["filter"].endswith("duration:[* TO 5]")
    provider.search("q", Kind.AUDIO_SFX, SearchFilters(kind=Kind.AUDIO_SFX, min_duration=2))
    assert route.calls.last.request.url.params["filter"].endswith("duration:[2 TO *]")


@respx.mock
def test_music_adds_tag_filter(provider: FreesoundProvider) -> None:
    route = mock_search()
    assets = provider.search("lofi", Kind.AUDIO_MUSIC, SearchFilters(kind=Kind.AUDIO_MUSIC))
    assert route.calls.last.request.url.params["filter"] == f"{DEFAULT_LICENSE_FILTER} tag:music"
    assert all(a.kind == Kind.AUDIO_MUSIC for a in assets)


@respx.mock
def test_mapping_license_attribution_duration(provider: FreesoundProvider) -> None:
    mock_search()
    assets = provider.search("q", Kind.AUDIO_SFX, SFX_FILTERS)
    # the result without previews is skipped
    assert [a.id for a in assets] == ["612345", "498761", "377001"]
    cc0, by, nc = assets
    for asset in assets:
        assert asset.provider == "freesound"
        assert asset.kind == Kind.AUDIO_SFX
        assert asset.source_url.startswith("https://freesound.org/people/")

    assert cc0.title == "Door Slam Heavy"
    assert cc0.license == "CC0 1.0"
    assert cc0.commercial_ok is True
    assert cc0.attribution_text == '"Door Slam Heavy" by foleyguy on Freesound (CC0 1.0)'
    assert cc0.duration_s == 1.84
    assert cc0.preview_url == "https://cdn.freesound.org/displays/612/612345_1234_wave_M.png"
    assert cc0.media_url == "https://cdn.freesound.org/previews/612/612345_1234-lq.mp3"
    assert cc0.media_type == "audio/mpeg"
    # no images in the fixture -> no thumbnail
    assert by.preview_url is None
    assert by.media_url == "https://cdn.freesound.org/previews/498/498761_5678-lq.mp3"
    assert by.media_type == "audio/mpeg"
    assert cc0.extra == {
        "download_url": "https://cdn.freesound.org/previews/612/612345_1234-hq.mp3",
        "original_type": "wav",
        "license_url": "http://creativecommons.org/publicdomain/zero/1.0/",
    }

    assert by.license == "CC BY 4.0"
    assert by.commercial_ok is True
    assert by.attribution_text == '"Whoosh Transition" by swooshy on Freesound (CC BY 4.0)'
    assert by.duration_s == 0.72

    assert nc.license == "CC BY-NC 3.0"
    assert nc.commercial_ok is False
    assert nc.duration_s == 12.0


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("http://creativecommons.org/licenses/by/3.0/", "CC BY 3.0"),
        ("http://creativecommons.org/licenses/sampling+/1.0/", "Sampling+"),
        ("Creative Commons 0", "CC0 1.0"),
        ("Attribution NonCommercial", "CC BY-NC"),
        ("https://example.com/weird-license", "https://example.com/weird-license"),
    ],
)
def test_license_label(raw: str, label: str) -> None:
    assert _license_label(raw) == label


@respx.mock
def test_resolve_returns_hq_mp3(provider: FreesoundProvider) -> None:
    mock_search()
    asset = provider.search("q", Kind.AUDIO_SFX, SFX_FILTERS)[0]
    plan = provider.resolve(asset)
    assert plan.method == "http"
    assert plan.url == "https://cdn.freesound.org/previews/612/612345_1234-hq.mp3"
    assert plan.ext == "mp3"
    assert plan.asset == asset


@respx.mock
def test_unsupported_kind_returns_empty_without_http(provider: FreesoundProvider) -> None:
    route = respx.get(url__startswith="https://freesound.org/")
    assert provider.search("q", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO)) == []
    assert route.call_count == 0


@respx.mock
def test_401_raises_provider_error_mentioning_key(provider: FreesoundProvider) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(401, json={"detail": f"Invalid token {KEY}"})
    )
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.AUDIO_SFX, SFX_FILTERS)
    assert info.value.provider == "freesound"
    assert "key" in info.value.message
    assert KEY not in str(info.value)


@respx.mock
def test_http_error_raises_provider_error_scrubbed(provider: FreesoundProvider) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(500, text=f"boom {KEY}"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.AUDIO_SFX, SFX_FILTERS)
    assert "500" in info.value.message
    assert KEY not in str(info.value)


@respx.mock
def test_transport_error_raises_provider_error(provider: FreesoundProvider) -> None:
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectError(f"failed Token {KEY}"))
    with pytest.raises(ProviderError) as info:
        provider.search("q", Kind.AUDIO_SFX, SFX_FILTERS)
    assert "ConnectError" in info.value.message
    assert KEY not in str(info.value)


@respx.mock
def test_missing_key_raises_provider_error(
    monkeypatch: pytest.MonkeyPatch, client: httpx.Client
) -> None:
    monkeypatch.delenv("CLIPFORAGE_FREESOUND_KEY", raising=False)
    route = respx.get(url__startswith="https://freesound.org/")
    with pytest.raises(ProviderError) as info:
        FreesoundProvider(client=client).search("q", Kind.AUDIO_SFX, SFX_FILTERS)
    assert info.value.provider == "freesound"
    assert "CLIPFORAGE_FREESOUND_KEY" in info.value.message
    assert route.call_count == 0


@respx.mock
def test_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch, client: httpx.Client) -> None:
    monkeypatch.setenv("CLIPFORAGE_FREESOUND_KEY", "env-key")
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    assert FreesoundProvider(client=client).search("q", Kind.AUDIO_SFX, SFX_FILTERS) == []
    assert route.calls.last.request.headers["Authorization"] == "Token env-key"


def test_satisfies_protocol() -> None:
    assert isinstance(FreesoundProvider(api_key=KEY), Provider)


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("CLIPFORAGE_FREESOUND_KEY"), reason="no Freesound key")
def test_live_search() -> None:
    assets = FreesoundProvider(page_size=3).search("door", Kind.AUDIO_SFX, SFX_FILTERS)
    assert len(assets) >= 1
    assert all(a.commercial_ok for a in assets)
