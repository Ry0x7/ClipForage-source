import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from clipforage import recall, recall_online, scrape, service
from clipforage.cache import Cache
from clipforage.cli import app
from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers import registry
from clipforage.providers.base import ProviderError
from clipforage.recall_online import RecallHit, SitemapSlugs, hit_names, kym_lookup, parse_entry

FIXTURES = Path(__file__).parent / "fixtures" / "knowyourmeme"
ROBOTS_URL = "https://knowyourmeme.com/robots.txt"
CRYING_CAT_URL = "https://knowyourmeme.com/memes/crying-cat"
SITEMAP_URL = "https://knowyourmeme.com/memes/confirmed/sitemap.xml"
SITEMAP_KEY = "knowyourmeme:_sitemap_v3"
WOMAN_CAT_URL = "https://knowyourmeme.com/memes/woman-yelling-at-a-cat"
WOMAN_CAT_HTML = (
    "<html><head></head><body>"
    "<h1 class='content-title entry-title'>Woman Yelling at a Cat</h1>"
    '<h2 id="about">About</h2><p><strong>Woman Yelling at a Cat</strong>, also '
    "<strong>Smudge Lord</strong>, is a meme.</p></body></html>"
)
runner = CliRunner()


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class NoSleep:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def join_prefetch() -> None:
    """Wait for a background sitemap fetch; call it inside the respx mock that serves it."""
    thread = recall_online.prefetch_thread()
    if thread is not None:
        thread.join(10)
        assert not thread.is_alive()


@pytest.fixture(autouse=True)
def _no_prefetch_leak() -> Iterator[None]:
    """No background sitemap fetch survives a test (or leaks in from another).

    A fetch still running here has outlived the test's respx mock and would reach the real
    network, so it fails the test (join it with ``join_prefetch`` inside the test).
    """
    recall_online.reset_prefetch()
    yield
    thread = recall_online.prefetch_thread()
    recall_online.reset_prefetch(timeout=5)
    assert thread is None or not thread.is_alive(), "background sitemap fetch outlived the test"


@pytest.fixture
def scrape_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFORAGE_SCRAPE_ENABLED", "1")
    clock = NoSleep()
    # keep the process-wide throttle from really sleeping in tests
    monkeypatch.setattr(
        scrape, "THROTTLE", scrape.HostThrottle(1.0, clock=clock, sleep=clock.sleep)
    )


@pytest.fixture
def no_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the raw query is looked up (no offline recall candidates)."""
    monkeypatch.setattr(recall, "expand_query", lambda *a, **k: [])


def mock_kym() -> tuple[respx.Route, respx.Route]:
    robots = respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text=fixture("robots.txt"))
    )
    respx.get(SITEMAP_URL).mock(
        return_value=httpx.Response(
            200, text=fixture("sitemap.xml"), headers={"Content-Type": "application/xml"}
        )
    )
    entry = respx.get(CRYING_CAT_URL).mock(
        return_value=httpx.Response(200, text=fixture("crying_cat.html"))
    )
    respx.get(url__regex=r"https://knowyourmeme\.com/memes/(?!crying-cat$).+").mock(
        return_value=httpx.Response(404, text=fixture("not_found.html"))
    )
    return robots, entry


def test_parse_entry_title_and_aliases() -> None:
    hit = parse_entry(fixture("crying_cat.html"), "https://x")
    assert hit == RecallHit(
        name="Crying Cat",
        aliases=["Sad Cat", "Schmuserkadser"],
        source_url=CRYING_CAT_URL,
    )


def test_parse_entry_without_title_is_none() -> None:
    assert parse_entry("<html><body>nothing</body></html>", "https://x") is None


def test_hit_names_skips_query_and_dupes() -> None:
    hits = [RecallHit("Crying Cat", ["Sad Cat", "crying cat", "Schmuserkadser"], "u")]
    assert hit_names(hits, "crying cat", limit=2) == ["Sad Cat", "Schmuserkadser"]
    assert hit_names(hits, "sad kitty", limit=5) == ["Crying Cat", "Sad Cat", "Schmuserkadser"]


@respx.mock
def test_kym_lookup_hit(scrape_on: None, no_offline: None) -> None:
    _, entry = mock_kym()
    hits = kym_lookup("Crying Cat!", client=httpx.Client())
    assert [h.name for h in hits] == ["Crying Cat"]
    assert hits[0].aliases == ["Sad Cat", "Schmuserkadser"]
    assert entry.call_count == 1


@respx.mock
def test_kym_lookup_404_is_no_hit(scrape_on: None, no_offline: None) -> None:
    mock_kym()
    assert kym_lookup("definitely not a real meme xyz", client=httpx.Client()) == []


@respx.mock
def test_kym_lookup_tries_offline_candidates(
    scrape_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_kym()
    monkeypatch.setattr(
        recall, "expand_query", lambda q, k=None, max_expansions=3: ["Crying Cat", "Other"]
    )
    hits = kym_lookup("sad kitty tears", client=httpx.Client())
    assert [h.name for h in hits] == ["Crying Cat"]
    requested = [c.request.url.path for c in respx.calls if "/memes/" in c.request.url.path]
    assert requested == ["/memes/sad-kitty-tears", "/memes/crying-cat", "/memes/other"]


@respx.mock
def test_kym_lookup_never_requests_search(scrape_on: None, no_offline: None) -> None:
    mock_kym()
    kym_lookup("crying cat", client=httpx.Client())
    assert all(not c.request.url.path.startswith("/search") for c in respx.calls)


@respx.mock
def test_kym_lookup_caches_hits_and_misses(scrape_on: None, no_offline: None) -> None:
    _, entry = mock_kym()
    with Cache() as cache:
        first = kym_lookup("crying cat", cache=cache, client=httpx.Client())
        again = kym_lookup("crying cat", cache=cache, client=httpx.Client())
        assert first == again
        assert entry.call_count == 1
        assert kym_lookup("nope nope", cache=cache, client=httpx.Client()) == []
        calls = len(respx.calls)
        assert kym_lookup("nope nope", cache=cache, client=httpx.Client()) == []
        assert len(respx.calls) == calls  # the miss was cached too
        assert cache.get_blob("knowyourmeme:crying-cat") is not None
        assert cache.get_blob("knowyourmeme:crying-cat", now=1e12) is None  # 7-day TTL
        join_prefetch()  # the first lookup started the background sitemap fetch


@respx.mock
def test_kym_lookup_server_error_raises(scrape_on: None, no_offline: None) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=fixture("robots.txt")))
    respx.get(CRYING_CAT_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderError) as info:
        kym_lookup("crying cat", client=httpx.Client())
    assert info.value.provider == "knowyourmeme"


def _paths(prefix: str = "/memes/") -> list[str]:
    return [c.request.url.path for c in respx.calls if c.request.url.path.startswith(prefix)]


@respx.mock
def test_load_slugs_parses_and_caches(scrape_on: None) -> None:
    mock_kym()
    with Cache() as cache:
        slugs = recall_online.load_slugs(cache, httpx.Client(), block=True)
        assert slugs.memes[:3] == ["crying-cat", "this-is-fine", "dog-in-a-burning-house"]
        assert len(slugs.memes) == 10  # /memes/people/... and percent-encoded slugs skipped
        assert slugs.memes[-1] == "horse_ebooks"  # "_" occurs in real entry slugs
        assert slugs.sensitive == ["woman-yelling-at-a-cat", "some-sensitive-entry"]
        assert recall_online.load_slugs(cache, httpx.Client(), block=True) == slugs
        assert _paths("/memes/confirmed/") == ["/memes/confirmed/sitemap.xml"]
        stored = cache.get_blob(SITEMAP_KEY)
        assert stored is not None
        assert set(json.loads(stored)) == {"memes", "sensitive"}
        assert cache.get_blob(SITEMAP_KEY, now=1e12) is None  # 7-day TTL


@respx.mock
def test_load_slugs_ignores_a_stale_list_payload(scrape_on: None) -> None:
    mock_kym()
    with Cache() as cache:
        cache.set_blob(SITEMAP_KEY, json.dumps(["crying-cat"]), 3600)
        slugs = recall_online.load_slugs(cache, httpx.Client(), block=True)
        assert "woman-yelling-at-a-cat" in slugs.sensitive  # refetched, not the stale list
        assert _paths("/memes/confirmed/") == ["/memes/confirmed/sitemap.xml"]


@respx.mock
def test_load_slugs_follows_a_small_index(scrape_on: None) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=fixture("robots.txt")))
    child = "https://knowyourmeme.com/memes/confirmed/sitemap-1.xml"
    index = (
        "<?xml version='1.0' encoding='utf-8' ?><sitemapindex>"
        f"<sitemap><loc>{child}</loc></sitemap>"
        "<sitemap><loc>https://knowyourmeme.com/photos/trending/sitemap.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=index))
    respx.get(child).mock(return_value=httpx.Response(200, text=fixture("sitemap.xml")))
    with Cache() as cache:
        slugs = recall_online.load_slugs(cache, httpx.Client(), block=True)
        assert (len(slugs.memes), len(slugs.sensitive)) == (10, 2)
    assert all("/photos/" not in c.request.url.path for c in respx.calls)


@respx.mock
def test_load_slugs_caps_slug_count(scrape_on: None) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=fixture("robots.txt")))
    urls = "".join(
        f"<url><loc>https://knowyourmeme.com/memes/m-{i}</loc></url>" for i in range(50_010)
    )
    respx.get(SITEMAP_URL).mock(return_value=httpx.Response(200, text=f"<urlset>{urls}</urlset>"))
    with Cache() as cache:
        slugs = recall_online.load_slugs(cache, httpx.Client(), block=True)
    assert len(slugs.memes) == 50_000
    assert slugs.memes[-1] == "m-49999"


@respx.mock
def test_load_slugs_caps_bytes(scrape_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_kym()
    text = fixture("sitemap.xml")
    monkeypatch.setattr(recall_online, "MAX_SITEMAP_BYTES", text.index("this-is-fine"))
    with Cache() as cache:
        assert recall_online.load_slugs(cache, httpx.Client(), block=True) == SitemapSlugs(
            memes=["crying-cat"], sensitive=["woman-yelling-at-a-cat"]
        )


@respx.mock
def test_load_slugs_error_is_empty_and_lookup_still_works(
    scrape_on: None, no_offline: None
) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=fixture("robots.txt")))
    sitemap = respx.get(SITEMAP_URL).mock(return_value=httpx.Response(503))
    respx.get(CRYING_CAT_URL).mock(
        return_value=httpx.Response(200, text=fixture("crying_cat.html"))
    )
    with Cache() as cache:
        assert recall_online.load_slugs(cache, httpx.Client(), block=True) == SitemapSlugs()
        hits = kym_lookup("crying cat", cache=cache, client=httpx.Client(), block_sitemap=True)
        assert [h.name for h in hits] == ["Crying Cat"]
        assert sitemap.call_count == 1  # the failure is remembered for an hour
        cache.set_blob(SITEMAP_KEY, '{"memes": [], "sensitive": []}', 0, now=0)  # expire it
        recall_online.load_slugs(cache, httpx.Client(), block=True)
        assert sitemap.call_count == 2


@respx.mock
def test_kym_lookup_fuzzy_slug(scrape_on: None, no_offline: None) -> None:
    # registered before mock_kym's catch-all 404 route (respx: first match wins)
    woman = respx.get(WOMAN_CAT_URL).mock(return_value=httpx.Response(200, text=WOMAN_CAT_HTML))
    mock_kym()

    def lookup(cache: Cache) -> list[RecallHit]:
        return kym_lookup(
            "woman yells at cat",
            cache=cache,
            client=httpx.Client(),
            safe=False,
            block_sitemap=True,
        )

    with Cache() as cache:
        hits = lookup(cache)
        assert [h.name for h in hits] == ["Woman Yelling at a Cat"]
        assert hits[0].aliases == ["Smudge Lord"]
        assert woman.call_count == 1
        assert _paths()[:3] == [
            "/memes/confirmed/sitemap.xml",
            "/memes/woman-yells-at-cat",
            "/memes/woman-yelling-at-a-cat",
        ]
        # second lookup: entries and sitemap come from the cache
        calls = len(respx.calls)
        assert lookup(cache) == hits
        assert len(respx.calls) == calls
    assert all(not c.request.url.path.startswith("/search") for c in respx.calls)


@respx.mock
def test_kym_lookup_fuzzy_is_bounded(scrape_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_kym()
    monkeypatch.setattr(recall, "expand_query", lambda *a, **k: ["Grumpy Cat", "Doge"])
    with Cache() as cache:
        kym_lookup("cat cat cat", cache=cache, client=httpx.Client(), block_sitemap=True)
    entries = [p for p in _paths() if not p.startswith("/memes/confirmed/")]
    assert len(entries) <= 1 + 2 + 3
    assert entries[:3] == ["/memes/cat-cat-cat", "/memes/grumpy-cat", "/memes/doge"]
    assert len(entries) == len(set(entries))


def test_fuzzy_slugs_ranking_and_rules() -> None:
    slugs = ["cat-woman", "woman-yelling-at-a-cat", "the-meme", "a-of-in", "grumpy-cat", "doge"]
    assert recall_online.fuzzy_slugs("woman yells at cat", slugs, limit=2) == [
        "woman-yelling-at-a-cat",
        "cat-woman",
    ]
    # stopwords and words under 3 letters never count as a match
    assert recall_online.fuzzy_slugs("the meme of a", slugs) == []
    assert recall_online.fuzzy_slugs("ox", ["ox", "ox-cart"]) == []
    assert recall_online.fuzzy_slugs(
        "grumpy cat", [*slugs, "grumpy-old-cat"], exclude=["grumpy-cat"]
    ) == ["grumpy-old-cat"]
    assert recall_online.fuzzy_slugs("doge", slugs, limit=0) == []
    # one key word: sharing it is enough
    assert recall_online.fuzzy_slugs("grumpy", slugs) == ["grumpy-cat"]


def test_fuzzy_slugs_needs_two_shared_words_for_multi_word_queries() -> None:
    burning = ["spongebob-burning-paper", "burning-piano", "dog-in-a-burning-house"]
    assert recall_online.fuzzy_slugs("dog in burning room", burning) == ["dog-in-a-burning-house"]
    cats = ["marsey-the-cat", "woman-in-black", "woman-yelling-at-a-cat"]
    assert recall_online.fuzzy_slugs("woman yells at cat", cats) == ["woman-yelling-at-a-cat"]
    assert recall_online.fuzzy_slugs("dog in burning room", burning[:2]) == []


def test_fuzzy_slugs_keeps_a_close_misspelling() -> None:
    slugs = ["distracted-girlfriend", "boyfriend-material", "distracted-boyfriend"]
    # one shared word, but the whole slug is a near spelling (ratio >= 0.8); the girlfriend
    # slug also shares "distracted" but is further off (ratio ~0.78)
    assert recall_online.fuzzy_slugs("distracted boyfreind", slugs) == ["distracted-boyfriend"]


def test_fuzzy_slugs_splits_underscore_slugs_but_returns_them_unchanged() -> None:
    slugs = ["horse_ebooks", "horse-armor", "ebooks-for-sale"]
    assert recall_online.fuzzy_slugs("horse ebooks", slugs) == ["horse_ebooks"]


@respx.mock
def test_kym_lookup_sensitive_fuzzy_only_with_safe_off(scrape_on: None, no_offline: None) -> None:
    woman = respx.get(WOMAN_CAT_URL).mock(return_value=httpx.Response(200, text=WOMAN_CAT_HTML))
    mock_kym()
    with Cache() as cache:
        assert kym_lookup("woman yells at cat", cache=cache, block_sitemap=True) == []
        assert woman.call_count == 0
        hits = kym_lookup("woman yells at cat", cache=cache, safe=False, block_sitemap=True)
        assert [h.name for h in hits] == ["Woman Yelling at a Cat"]
        assert woman.call_count == 1


@respx.mock
def test_kym_lookup_non_blocking_prefetches_in_background(
    scrape_on: None, no_offline: None
) -> None:
    release = threading.Event()
    started = threading.Event()

    def slow_sitemap(request: httpx.Request) -> httpx.Response:
        started.set()
        assert release.wait(10), "test never released the sitemap"
        return httpx.Response(200, text=fixture("sitemap.xml"))

    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=fixture("robots.txt")))
    sitemap = respx.get(SITEMAP_URL).mock(side_effect=slow_sitemap)
    woman = respx.get(WOMAN_CAT_URL).mock(return_value=httpx.Response(200, text=WOMAN_CAT_HTML))
    respx.get(url__regex=r"https://knowyourmeme\.com/memes/.+").mock(
        return_value=httpx.Response(404, text=fixture("not_found.html"))
    )
    with Cache() as cache:
        # empty cache: only the exact slug is tried, the sitemap is fetched in the background
        assert kym_lookup("woman yells at cat", cache=cache, safe=False) == []
        thread = recall_online.prefetch_thread()
        assert thread is not None and thread.daemon
        assert started.wait(10)
        assert cache.get_blob(SITEMAP_KEY) is None
        assert _paths("/memes/w") == ["/memes/woman-yells-at-cat"]
        # a second call while it is in flight does not start another fetch
        assert kym_lookup("woman yells at cat", cache=cache, safe=False) == []
        assert recall_online.prefetch_thread() is thread
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        assert sitemap.call_count == 1
        assert cache.get_blob(SITEMAP_KEY) is not None  # stored by the background thread
        hits = kym_lookup("woman yells at cat", cache=cache, safe=False)
        assert [h.name for h in hits] == ["Woman Yelling at a Cat"]
        assert woman.call_count == 1
        assert sitemap.call_count == 1
        assert recall_online.prefetch_thread() is thread  # no new thread: the cache was used


def test_kym_lookup_disabled_raises() -> None:
    with pytest.raises(ProviderError, match="scraping is disabled"):
        kym_lookup("crying cat")


@respx.mock
def test_kym_lookup_disabled_skips_sitemap() -> None:
    mock_kym()
    with Cache() as cache, pytest.raises(ProviderError, match="scraping is disabled"):
        kym_lookup("crying cat", cache=cache, client=httpx.Client())
    assert respx.calls.call_count == 0


# --- search_expanded hook -------------------------------------------------------------------


class QueryProvider:
    kinds = frozenset({Kind.VIDEO})
    requires_key = False

    def __init__(self, per_query: dict[str, list[str]]) -> None:
        self.id = "fake"
        self.per_query = per_query
        self.queries: list[str] = []

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        self.queries.append(query)
        return [
            Asset(
                provider=self.id,
                id=i,
                kind=kind,
                title=i,
                source_url=f"https://example.com/{i}",
                license="CC0-1.0",
                attribution_text="CC0",
                commercial_ok=True,
            )
            for i in self.per_query.get(query, [])
        ]

    def resolve(self, asset: Asset) -> DownloadPlan:
        raise NotImplementedError


@pytest.fixture
def fake_kym(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def lookup(query: str, kind: Kind | None = None, **kwargs: object) -> list[RecallHit]:
        calls.append(query)
        return [RecallHit("Crying Cat", ["Sad Cat", "Schmuserkadser"], CRYING_CAT_URL)]

    monkeypatch.setattr(registry, "load_builtins", lambda: None)
    monkeypatch.setattr(recall_online, "kym_lookup", lookup)
    monkeypatch.setattr(recall, "expand_query", lambda *a, **k: [])
    return calls


FILTERS = SearchFilters(kind=Kind.VIDEO)


def test_hook_runs_on_few_results_when_enabled(scrape_on: None, fake_kym: list[str]) -> None:
    fake = QueryProvider({"weepy kitty": ["a"], "Crying Cat": ["b"], "Sad Cat": ["c"]})
    registry.register(fake)
    outcome = service.search_expanded("weepy kitty", FILTERS, limit=10, use_cache=False)
    assert fake_kym == ["weepy kitty"]
    assert outcome.expanded_queries == ["Crying Cat", "Sad Cat"]  # max 2 online names
    assert [a.id for a in outcome.assets] == ["a", "b", "c"]
    assert outcome.errors == []


@pytest.mark.parametrize("safe", [True, False])
def test_hook_passes_safe_and_never_blocks(
    scrape_on: None, monkeypatch: pytest.MonkeyPatch, safe: bool
) -> None:
    seen: list[dict[str, object]] = []

    def lookup(query: str, kind: Kind | None = None, **kwargs: object) -> list[RecallHit]:
        seen.append(kwargs)
        return []

    monkeypatch.setattr(registry, "load_builtins", lambda: None)
    monkeypatch.setattr(recall_online, "kym_lookup", lookup)
    monkeypatch.setattr(recall, "expand_query", lambda *a, **k: [])
    registry.register(QueryProvider({"weepy kitty": []}))
    filters = SearchFilters(kind=Kind.VIDEO, safe=safe)
    service.search_expanded("weepy kitty", filters, limit=10, use_cache=False)
    assert len(seen) == 1
    assert seen[0]["safe"] is safe
    assert seen[0]["block_sitemap"] is False


def test_hook_skipped_with_three_results(scrape_on: None, fake_kym: list[str]) -> None:
    registry.register(QueryProvider({"weepy kitty": ["a", "b", "c"]}))
    outcome = service.search_expanded("weepy kitty", FILTERS, limit=10, use_cache=False)
    assert fake_kym == []
    assert outcome.expanded_queries == []


def test_hook_skipped_when_scraping_disabled(fake_kym: list[str]) -> None:
    registry.register(QueryProvider({"weepy kitty": []}))
    outcome = service.search_expanded("weepy kitty", FILTERS, limit=10, use_cache=False)
    assert fake_kym == []
    assert outcome.expanded_queries == []
    assert outcome.errors == []


def test_hook_skipped_when_expand_off(scrape_on: None, fake_kym: list[str]) -> None:
    registry.register(QueryProvider({"weepy kitty": []}))
    filters = SearchFilters(kind=Kind.VIDEO, expand=False)
    service.search_expanded("weepy kitty", filters, limit=10, use_cache=False)
    assert fake_kym == []


def test_hook_failure_is_reported_not_fatal(
    scrape_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> list[RecallHit]:
        raise ProviderError("knowyourmeme", "HTTP 503 for https://knowyourmeme.com/memes/x")

    monkeypatch.setattr(registry, "load_builtins", lambda: None)
    monkeypatch.setattr(recall_online, "kym_lookup", boom)
    registry.register(QueryProvider({"weepy kitty": ["a"]}))
    outcome = service.search_expanded("weepy kitty", FILTERS, limit=10, use_cache=False)
    assert [a.id for a in outcome.assets] == ["a"]
    assert [e.provider for e in outcome.errors] == ["knowyourmeme"]


# --- CLI and web ------------------------------------------------------------------------------


@respx.mock
def test_cli_recall_online(scrape_on: None, no_offline: None) -> None:
    mock_kym()
    result = runner.invoke(app, ["recall", "crying cat", "--online"])
    assert result.exit_code == 0, result.output
    assert (
        "Crying Cat (also: Sad Cat, Schmuserkadser)  [Know Your Meme: "
        "https://knowyourmeme.com/memes/crying-cat]"
    ) in result.output


NOTICE = "building Know Your Meme index (first run, about 40 s)"


@respx.mock
def test_cli_recall_online_unsafe_reaches_sensitive(scrape_on: None, no_offline: None) -> None:
    respx.get(WOMAN_CAT_URL).mock(return_value=httpx.Response(200, text=WOMAN_CAT_HTML))
    mock_kym()
    first = runner.invoke(app, ["recall", "woman yells at cat", "--online"])
    assert first.exit_code == 0, first.output
    assert NOTICE in first.stderr  # blocking build, announced once
    assert "Woman Yelling at a Cat" not in first.output
    unsafe = runner.invoke(app, ["recall", "woman yells at cat", "--online", "--unsafe"])
    assert unsafe.exit_code == 0, unsafe.output
    assert NOTICE not in unsafe.stderr  # the index is cached now
    assert "Woman Yelling at a Cat (also: Smudge Lord)" in unsafe.output
    assert recall_online.prefetch_thread() is None  # the CLI never prefetches in background


INDEX_NOTE = (
    'note: Know Your Meme index not built yet; run `clipforage recall --online "weepy kitty"` '
    "once (about 40 s) to enable meme-name suggestions"
)


@respx.mock
def test_cli_search_notes_index_while_prefetch_runs(
    scrape_on: None, no_offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def slow_sitemap(request: httpx.Request) -> httpx.Response:
        release.wait(10)  # keep the background fetch alive until the CLI has finished
        return httpx.Response(200, text=fixture("sitemap.xml"))

    mock_kym()
    respx.get(SITEMAP_URL).mock(side_effect=slow_sitemap)
    monkeypatch.setattr(registry, "load_builtins", lambda: None)
    registry.register(QueryProvider({"weepy kitty": ["a"]}))
    try:
        result = runner.invoke(app, ["search", "weepy kitty", "--no-cache"])
        assert recall_online.prefetch_running()
    finally:
        release.set()
        join_prefetch()
    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines().count(INDEX_NOTE) == 1
    assert not recall_online.prefetch_running()


@respx.mock
def test_cli_search_no_index_note_without_prefetch(
    scrape_on: None, no_offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_kym()
    with Cache() as cache:  # the index is already built
        cache.set_blob(SITEMAP_KEY, json.dumps({"memes": ["crying-cat"], "sensitive": []}), 60)
    monkeypatch.setattr(registry, "load_builtins", lambda: None)
    registry.register(QueryProvider({"weepy kitty": ["a"]}))
    result = runner.invoke(app, ["search", "weepy kitty", "--no-cache"])
    assert result.exit_code == 0, result.output
    assert recall_online.prefetch_thread() is None
    assert "Know Your Meme index not built" not in result.output
    assert "Know Your Meme index not built" not in result.stderr


def test_cli_recall_online_disabled_warns() -> None:
    result = runner.invoke(app, ["recall", "dog in burning room", "--online"])
    assert result.exit_code == 0
    assert "this is fine" in result.output.lower()  # offline suggestion still printed
    assert "online lookup skipped" in result.output


def test_cli_recall_without_online_makes_no_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> list[RecallHit]:
        raise AssertionError("online lookup without --online")

    monkeypatch.setattr(service, "online_recall", fail)
    assert runner.invoke(app, ["recall", "crying cat"]).exit_code == 0


@pytest.fixture
def web() -> TestClient:
    from clipforage.web.app import create_app

    return TestClient(create_app(), base_url="http://127.0.0.1:8765")


@respx.mock
def test_web_recall_online(web: TestClient, scrape_on: None, no_offline: None) -> None:
    mock_kym()
    body = web.get("/api/recall", params={"q": "crying cat", "online": "true"}).json()
    assert body == {"suggestions": ["Sad Cat", "Schmuserkadser"]}
    join_prefetch()  # the web endpoint never blocks: the sitemap was fetched in the background


@respx.mock
def test_web_recall_safe_false_reaches_sensitive(
    web: TestClient, scrape_on: None, no_offline: None
) -> None:
    respx.get(WOMAN_CAT_URL).mock(return_value=httpx.Response(200, text=WOMAN_CAT_HTML))
    mock_kym()
    params = {"q": "woman yells at cat", "online": "true", "safe": "false"}
    # non-blocking: the first call starts the background sitemap fetch and has no fuzzy hit
    assert web.get("/api/recall", params=params).json() == {"suggestions": []}
    assert recall_online.prefetch_thread() is not None
    join_prefetch()
    body = web.get("/api/recall", params=params).json()
    assert body == {"suggestions": ["Woman Yelling at a Cat", "Smudge Lord"]}
    safe_on = web.get("/api/recall", params={**params, "safe": "true"}).json()
    assert safe_on == {"suggestions": []}


def test_web_recall_offline_by_default(web: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> list[RecallHit]:
        raise AssertionError("online lookup without online=true")

    monkeypatch.setattr(service, "online_recall", fail)
    assert web.get("/api/recall", params={"q": "crying cat"}).status_code == 200


def test_web_recall_online_disabled_keeps_offline(web: TestClient) -> None:
    body = web.get("/api/recall", params={"q": "dog in burning room", "online": "true"}).json()
    assert body["suggestions"] == recall.expand_query("dog in burning room")
