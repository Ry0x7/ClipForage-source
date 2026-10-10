import threading
from pathlib import Path

import httpx
import pytest
import respx

from clipforage import config, scrape
from clipforage.http import USER_AGENT
from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers import registry
from clipforage.providers.base import ProviderError

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    """A monotonic clock that only moves when ``sleep`` is called."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def throttle(clock: FakeClock) -> scrape.HostThrottle:
    return scrape.HostThrottle(1.0, clock=clock, sleep=clock.sleep)


@pytest.fixture
def scrape_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_ENABLED", "1")


def test_slugify() -> None:
    assert scrape.slugify("Cat") == "cat"
    assert scrape.slugify("  Crying   Cat!! ") == "crying-cat"
    assert scrape.slugify("rock & roll / 80's") == "rock-roll-80-s"
    assert scrape.slugify("--__--") == ""


def test_throttle_spaces_requests_per_host(clock: FakeClock, throttle: scrape.HostThrottle) -> None:
    assert throttle.wait("mixkit.co") == 0.0
    assert throttle.wait("mixkit.co") == pytest.approx(1.0)
    assert clock.now == pytest.approx(101.0)
    # another host is independent
    assert throttle.wait("knowyourmeme.com") == 0.0
    clock.now += 5.0  # idle long enough: no wait
    assert throttle.wait("mixkit.co") == 0.0
    assert clock.sleeps == [pytest.approx(1.0)]


def test_throttle_partial_wait(clock: FakeClock, throttle: scrape.HostThrottle) -> None:
    throttle.wait("h")
    clock.now += 0.4
    assert throttle.wait("h") == pytest.approx(0.6)


def test_throttle_is_thread_safe() -> None:
    lock = threading.Lock()
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        with lock:
            slept.append(seconds)

    throttle = scrape.HostThrottle(1.0, clock=lambda: now[0], sleep=sleep)
    threads = [threading.Thread(target=throttle.wait, args=("h",)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # five callers at the same instant get five distinct slots 0, 1, 2, 3, 4 s out
    assert sorted(slept) == [pytest.approx(x) for x in (1.0, 2.0, 3.0, 4.0)]


def test_min_interval_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert scrape.min_interval_s() == 1.0
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_MIN_INTERVAL_S", "2.5")
    assert scrape.min_interval_s() == 2.5
    assert scrape.HostThrottle().interval_s == 2.5
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_MIN_INTERVAL_S", "0.1")
    assert scrape.min_interval_s() == 1.0  # never faster than 1 request/s per host
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_MIN_INTERVAL_S", "fast")
    assert scrape.min_interval_s() == 1.0


def test_enabled_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIPFORAGE_SCRAPE_ENABLED")
    assert scrape.enabled() is True  # default on
    config.set_setting("scrape", "enabled", False)
    assert scrape.enabled() is False
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_ENABLED", "yes")  # env wins over config.toml
    assert scrape.enabled() is True
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_ENABLED", "off")
    assert scrape.enabled() is False


def test_get_bool_setting_ignores_non_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    config.set_setting("scrape", "enabled", "nope")
    assert config.get_bool_setting("scrape", "enabled", default=True) is True
    monkeypatch.setenv("CLIPFORAGE_X_FLAG", "maybe")
    assert config.get_bool_setting("scrape", "enabled", env="X_FLAG", default=False) is False


@respx.mock
def test_robots_allows_and_caches(throttle: scrape.HostThrottle) -> None:
    route = respx.get("https://knowyourmeme.com/robots.txt").mock(
        return_value=httpx.Response(
            200, text=(FIXTURES / "knowyourmeme" / "robots.txt").read_text(encoding="utf-8")
        )
    )
    client = httpx.Client()
    assert scrape.robots_allows(
        "https://knowyourmeme.com/memes/crying-cat", client=client, throttle=throttle
    )
    assert not scrape.robots_allows(
        "https://knowyourmeme.com/search?q=cat", client=client, throttle=throttle
    )
    assert route.call_count == 1  # fetched once per host per process


@respx.mock
@pytest.mark.parametrize(
    ("status", "allowed"), [(404, True), (410, True), (403, False), (401, False), (503, True)]
)
def test_robots_status_handling(status: int, allowed: bool, throttle: scrape.HostThrottle) -> None:
    respx.get("https://example.org/robots.txt").mock(return_value=httpx.Response(status))
    assert (
        scrape.robots_allows("https://example.org/page", client=httpx.Client(), throttle=throttle)
        is allowed
    )


@respx.mock
def test_robots_fetch_failure_assumes_allowed(
    throttle: scrape.HostThrottle, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get("https://example.org/robots.txt").mock(side_effect=httpx.ConnectError("boom"))
    assert scrape.robots_allows("https://example.org/x", client=httpx.Client(), throttle=throttle)
    assert "assuming allowed" in caplog.text


@respx.mock
def test_fetch_html_sends_ua_accept_and_checks_status(throttle: scrape.HostThrottle) -> None:
    respx.get("https://example.org/robots.txt").mock(return_value=httpx.Response(404))
    page = respx.get("https://example.org/ok").mock(return_value=httpx.Response(200, text="<p>"))
    respx.get("https://example.org/gone").mock(return_value=httpx.Response(500))
    from clipforage import http

    client = http.make_client()
    assert scrape.fetch_html("https://example.org/ok", client=client, throttle=throttle) == "<p>"
    request = page.calls.last.request
    assert request.headers["User-Agent"] == USER_AGENT
    assert request.headers["Accept"] == scrape.ACCEPT_HTML
    with pytest.raises(scrape.ScrapeHTTPError) as info:
        scrape.fetch_html(
            "https://example.org/gone", provider="p", client=client, throttle=throttle
        )
    assert info.value.status == 500
    assert info.value.provider == "p"
    assert "HTTP 500" in info.value.message


@respx.mock
def test_fetch_html_refuses_robots_disallowed(throttle: scrape.HostThrottle) -> None:
    respx.get("https://example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    page = respx.get("https://example.org/private/x").mock(return_value=httpx.Response(200))
    with pytest.raises(ProviderError, match="robots.txt disallows /private/x"):
        scrape.fetch_html("https://example.org/private/x", client=httpx.Client(), throttle=throttle)
    assert not page.called


@respx.mock
def test_requests_to_same_host_are_throttled(
    clock: FakeClock, throttle: scrape.HostThrottle
) -> None:
    respx.get("https://example.org/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(url__regex=r"https://example\.org/p\d").mock(return_value=httpx.Response(200))
    client = httpx.Client()
    scrape.fetch_html("https://example.org/p1", client=client, throttle=throttle)
    scrape.fetch_html("https://example.org/p2", client=client, throttle=throttle)
    # robots.txt, p1, p2: each at least 1 s after the previous request to the host
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


class ScrapingFake:
    id = "scrapy"
    kinds = frozenset({Kind.VIDEO})
    requires_key = False
    scrapes = True

    def __init__(self) -> None:
        self.calls = 0

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        self.calls += 1
        return []

    def resolve(self, asset: Asset) -> DownloadPlan:
        raise NotImplementedError


def test_registry_skips_scraping_providers_when_disabled() -> None:
    fake = ScrapingFake()
    assets, errors = registry.search_all("q", SearchFilters(kind=Kind.VIDEO), [fake])
    assert assets == [] and fake.calls == 0
    assert [(e.provider, e.message) for e in errors] == [("scrapy", scrape.DISABLED_MESSAGE)]


def test_registry_runs_scraping_providers_when_enabled(scrape_on: None) -> None:
    fake = ScrapingFake()
    _, errors = registry.search_all("q", SearchFilters(kind=Kind.VIDEO), [fake])
    assert errors == [] and fake.calls == 1


@respx.mock
def test_redirects_are_followed_with_throttle_and_robots(
    clock: FakeClock, throttle: scrape.HostThrottle
) -> None:
    respx.get("https://example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    respx.get("https://example.org/door/").mock(
        return_value=httpx.Response(301, headers={"Location": "/discover/door/"})
    )
    final = respx.get("https://example.org/discover/door/").mock(
        return_value=httpx.Response(200, text="ok")
    )
    respx.get("https://example.org/sneaky").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.org/private/x"})
    )
    client = httpx.Client(follow_redirects=True)  # scrape overrides the client's setting
    assert scrape.fetch_html("https://example.org/door/", client=client, throttle=throttle) == "ok"
    assert final.call_count == 1
    assert len(clock.sleeps) == 2  # robots -> /door/ -> /discover/door/, each 1 s apart
    with pytest.raises(ProviderError, match="robots.txt disallows /private/x"):
        scrape.fetch_html("https://example.org/sneaky", client=client, throttle=throttle)


@respx.mock
def test_too_many_redirects(throttle: scrape.HostThrottle) -> None:
    respx.get("https://example.org/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.org/loop").mock(
        return_value=httpx.Response(302, headers={"Location": "/loop"})
    )
    with pytest.raises(ProviderError, match="too many redirects"):
        scrape.fetch_html("https://example.org/loop", client=httpx.Client(), throttle=throttle)
