import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforage import scrape
from clipforage.models import Kind, SearchFilters
from clipforage.providers import registry
from clipforage.providers.base import Provider, ProviderError
from clipforage.providers.lottiefiles import GRAPHQL_URL, LICENSE, LottieFilesProvider

FIXTURES = Path(__file__).parent / "fixtures" / "lottiefiles"
OVERLAY = SearchFilters(kind=Kind.OVERLAY)
TEMPLATE = SearchFilters(kind=Kind.TEMPLATE)
ROBOTS_URL = "https://graphql.lottiefiles.com/robots.txt"


def load() -> dict[str, Any]:
    return json.loads((FIXTURES / "search_loading.json").read_text(encoding="utf-8"))


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
def provider() -> LottieFilesProvider:
    clock = NoSleep()
    return LottieFilesProvider(
        client=httpx.Client(), throttle=scrape.HostThrottle(1.0, clock=clock, sleep=clock.sleep)
    )


def mock_api(response: httpx.Response | None = None) -> respx.Route:
    # The live endpoint answers 404 for robots.txt (recorded): everything allowed.
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(404, json={"statusCode": 404}))
    return respx.post(GRAPHQL_URL).mock(return_value=response or httpx.Response(200, json=load()))


def test_satisfies_protocol() -> None:
    p = LottieFilesProvider()
    assert isinstance(p, Provider)
    assert p.kinds == {Kind.OVERLAY, Kind.TEMPLATE}
    assert p.scrapes is True and p.requires_key is False
    assert p.SEARCH_TTL_S == 86400


@respx.mock
def test_posts_graphql_query(provider: LottieFilesProvider) -> None:
    route = mock_api()
    provider.search("loading", Kind.OVERLAY, OVERLAY.model_copy(update={"limit": 5}))
    body = json.loads(route.calls.last.request.content)
    assert "searchPublicAnimations" in body["query"]
    assert body["variables"] == {"q": "loading", "first": 5}


@respx.mock
def test_page_size_default_and_cap(provider: LottieFilesProvider) -> None:
    route = mock_api()
    provider.search("loading", Kind.OVERLAY, OVERLAY)
    assert json.loads(route.calls.last.request.content)["variables"]["first"] == 20
    provider.search("loading", Kind.OVERLAY, OVERLAY.model_copy(update={"limit": 500}))
    assert json.loads(route.calls.last.request.content)["variables"]["first"] == 50


@respx.mock
def test_overlay_maps_video_url(provider: LottieFilesProvider) -> None:
    mock_api()
    assets = provider.search("loading", Kind.OVERLAY, OVERLAY)
    assert len(assets) == 5
    first = assets[0]
    assert first.provider == "lottiefiles"
    assert first.id == "1504643"
    assert first.kind == Kind.OVERLAY
    assert first.title == "loading"
    assert first.source_url == "https://lottiefiles.com/animations/loading-zVjETvqAb9"
    assert first.media_url.endswith("/thcVEesYIS.mp4")
    assert first.media_type == "video/mp4"
    assert first.preview_url.endswith("/qhxRMLKNqb.png")
    assert first.license == LICENSE
    assert first.commercial_ok is None
    assert first.attribution_text == '"loading" by gegqvzcgru7w44rm on LottieFiles'
    assert first.duration_s == 2.0
    assert first.orientation == "landscape"
    assert assets[1].orientation == "square"
    plan = provider.resolve(first)
    assert (plan.method, plan.url, plan.ext) == ("http", first.media_url, "mp4")


@respx.mock
def test_template_maps_json_url(provider: LottieFilesProvider) -> None:
    mock_api()
    first = provider.search("loading", Kind.TEMPLATE, TEMPLATE)[0]
    assert first.kind == Kind.TEMPLATE
    assert first.media_url.endswith("/zVjETvqAb9.json")
    assert first.media_type == "application/json"
    plan = provider.resolve(first)
    assert (plan.url, plan.ext) == (first.media_url, "json")


@respx.mock
def test_graphql_errors_raise(provider: LottieFilesProvider) -> None:
    mock_api(httpx.Response(200, json={"errors": [{"message": "rate limited"}]}))
    with pytest.raises(ProviderError, match="GraphQL error: rate limited"):
        provider.search("loading", Kind.OVERLAY, OVERLAY)


@respx.mock
def test_http_error_and_bad_shape_raise(provider: LottieFilesProvider) -> None:
    mock_api(httpx.Response(403, text="gated"))
    with pytest.raises(ProviderError, match="HTTP 403"):
        provider.search("loading", Kind.OVERLAY, OVERLAY)
    respx.post(GRAPHQL_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
    with pytest.raises(ProviderError, match="no searchPublicAnimations"):
        provider.search("loading", Kind.OVERLAY, OVERLAY)
    respx.post(GRAPHQL_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError) as info:
        provider.search("loading", Kind.OVERLAY, OVERLAY)
    assert info.value.provider == "lottiefiles"


def test_disabled_scraping_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_ENABLED", "0")
    _, errors = registry.search_all("loading", OVERLAY, [LottieFilesProvider()])
    assert [e.provider for e in errors] == ["lottiefiles"]
    with pytest.raises(ProviderError, match="scraping is disabled"):
        LottieFilesProvider().search("loading", Kind.OVERLAY, OVERLAY)


def test_unsupported_kind_returns_empty(provider: LottieFilesProvider) -> None:
    assert provider.search("x", Kind.VIDEO, SearchFilters(kind=Kind.VIDEO)) == []
