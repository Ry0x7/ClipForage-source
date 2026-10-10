import json
import os
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforage.models import Kind, SearchFilters
from clipforage.providers.base import Provider, ProviderError
from clipforage.providers.google_fonts import WEBFONTS_URL, GoogleFontsProvider

FIXTURES = Path(__file__).parent / "fixtures" / "google_fonts"
KEY = "test-google-fonts-key"
FILTERS = SearchFilters(kind=Kind.FONT)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def client() -> httpx.Client:
    return httpx.Client()


@pytest.fixture
def provider(client: httpx.Client) -> GoogleFontsProvider:
    return GoogleFontsProvider(api_key=KEY, client=client)


def mock_list() -> respx.Route:
    return respx.get(WEBFONTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("webfonts.json"))
    )


@respx.mock
def test_search_request_params_and_mapping(provider: GoogleFontsProvider) -> None:
    route = mock_list()
    assets = provider.search("roboto", Kind.FONT, FILTERS)

    params = route.calls.last.request.url.params
    assert params["key"] == KEY
    assert params["sort"] == "popularity"
    assert [a.title for a in assets] == ["Roboto", "Roboto Slab"]
    roboto, slab = assets
    assert roboto.license == "OFL-1.1"
    assert roboto.id == "roboto"
    assert slab.license == "Apache-2.0"
    assert slab.id == "robotoslab"
    assert slab.provider == "google_fonts"
    assert slab.kind == Kind.FONT
    assert slab.commercial_ok is True
    assert slab.preview_url is None
    assert slab.source_url == "https://fonts.google.com/specimen/Roboto+Slab"
    assert slab.attribution_text == "Roboto Slab font, Apache-2.0 (Google Fonts)"
    assert slab.extra["category"] == "serif"
    assert slab.extra["version"] == "v34"
    assert slab.extra["subsets"] == ["latin"]
    assert slab.extra["variants"] == ["100", "700"]


@respx.mock
def test_ubuntu_is_ufl(provider: GoogleFontsProvider) -> None:
    mock_list()
    assets = provider.search("UBUNTU", Kind.FONT, FILTERS)
    assert [(a.title, a.license) for a in assets] == [("Ubuntu", "UFL-1.0")]


@respx.mock
def test_category_query_matches_by_category(provider: GoogleFontsProvider) -> None:
    mock_list()
    assets = provider.search("handwriting", Kind.FONT, FILTERS)
    assert [a.title for a in assets] == ["Permanent Marker"]
    assert assets[0].license == "Apache-2.0"


@respx.mock
def test_multi_token_query_requires_all_tokens(provider: GoogleFontsProvider) -> None:
    mock_list()
    assets = provider.search("slab roboto", Kind.FONT, FILTERS)
    assert [a.title for a in assets] == ["Roboto Slab"]


@respx.mock
def test_empty_query_returns_api_order_capped(client: httpx.Client) -> None:
    mock_list()
    provider = GoogleFontsProvider(api_key=KEY, client=client, max_results=3)
    assets = provider.search("", Kind.FONT, FILTERS)
    assert [a.title for a in assets] == ["Roboto", "Inter", "Roboto Slab"]


@respx.mock
def test_max_results_caps_matches(client: httpx.Client) -> None:
    mock_list()
    provider = GoogleFontsProvider(api_key=KEY, client=client, max_results=1)
    assets = provider.search("roboto", Kind.FONT, FILTERS)
    assert [a.title for a in assets] == ["Roboto"]


@respx.mock
def test_list_is_memoized_per_instance(provider: GoogleFontsProvider) -> None:
    route = mock_list()
    provider.search("roboto", Kind.FONT, FILTERS)
    assets = provider.search("lobster", Kind.FONT, FILTERS)
    assert route.call_count == 1
    assert [a.title for a in assets] == ["Lobster"]


@respx.mock
def test_files_upgraded_to_https(provider: GoogleFontsProvider) -> None:
    mock_list()
    roboto = provider.search("roboto", Kind.FONT, FILTERS)[0]
    files = roboto.extra["files"]
    assert set(files) == {"700", "regular", "italic"}
    assert all(url.startswith("https://fonts.gstatic.com/") for url in files.values())


@respx.mock
def test_resolve_regular_ttf(provider: GoogleFontsProvider) -> None:
    mock_list()
    roboto, slab = provider.search("roboto", Kind.FONT, FILTERS)
    plan = provider.resolve(roboto)
    assert plan.method == "http"
    assert plan.url == "https://fonts.gstatic.com/s/roboto/v47/KFOmCnqEu92Fr1Me5Q.ttf"
    assert plan.ext == "ttf"
    # no "regular" style: falls back to the first listed file
    slab_plan = provider.resolve(slab)
    assert slab_plan.url == slab.extra["files"]["100"]
    assert slab_plan.url.startswith("https://")
    assert slab_plan.ext == "ttf"


def test_missing_key_raises(client: httpx.Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIPFORAGE_GOOGLE_FONTS_KEY", raising=False)
    provider = GoogleFontsProvider(client=client)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(WEBFONTS_URL)
        with pytest.raises(ProviderError) as exc:
            provider.search("roboto", Kind.FONT, FILTERS)
        assert route.call_count == 0
    assert exc.value.provider == "google_fonts"
    assert "CLIPFORAGE_GOOGLE_FONTS_KEY" in exc.value.message


def test_key_from_env(client: httpx.Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_GOOGLE_FONTS_KEY", "env-key")
    with respx.mock:
        route = mock_list()
        GoogleFontsProvider(client=client).search("inter", Kind.FONT, FILTERS)
    assert route.calls.last.request.url.params["key"] == "env-key"


@respx.mock
def test_403_raises_without_key(provider: GoogleFontsProvider) -> None:
    respx.get(WEBFONTS_URL).mock(
        return_value=httpx.Response(403, json={"error": {"message": f"bad key {KEY}"}})
    )
    with pytest.raises(ProviderError) as exc:
        provider.search("roboto", Kind.FONT, FILTERS)
    assert KEY not in str(exc.value)
    assert "403" in exc.value.message
    assert "key" in exc.value.message.lower()


@respx.mock
def test_transport_error_scrubs_key(provider: GoogleFontsProvider) -> None:
    respx.get(WEBFONTS_URL).mock(side_effect=httpx.ConnectError(f"boom ?key={KEY}"))
    with pytest.raises(ProviderError) as exc:
        provider.search("roboto", Kind.FONT, FILTERS)
    assert KEY not in str(exc.value)
    assert exc.value.__cause__ is None


def test_unsupported_kind_returns_empty_without_request(provider: GoogleFontsProvider) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(WEBFONTS_URL)
        assert provider.search("roboto", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO)) == []
        assert route.call_count == 0


def test_license_map_loads_from_package_data() -> None:
    text = (
        resources.files("clipforage.providers.data")
        .joinpath("google_fonts_licenses.json")
        .read_text(encoding="utf-8")
    )
    data = json.loads(text)
    assert data["ubuntu"] == "UFL-1.0"
    assert data["robotoslab"] == "Apache-2.0"
    assert set(data.values()) <= {"Apache-2.0", "UFL-1.0", "CC-BY-SA-4.0"}


def test_implements_protocol(provider: GoogleFontsProvider) -> None:
    assert isinstance(provider, Provider)
    assert provider.id == "google_fonts"
    assert provider.kinds == frozenset({Kind.FONT})
    assert provider.requires_key is True
    assert provider.SEARCH_TTL_S == 86400


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("CLIPFORAGE_GOOGLE_FONTS_KEY"), reason="no Google Fonts key")
def test_live_search() -> None:
    assets = GoogleFontsProvider(max_results=3).search("roboto", Kind.FONT, FILTERS)
    assert len(assets) >= 1
    assert all(a.license == "OFL-1.1" for a in assets)
