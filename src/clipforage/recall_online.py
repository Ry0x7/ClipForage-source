"""Online meme-name lookup on Know Your Meme entry pages (``recall`` is the offline half).

``kym_lookup`` slugifies the query (plus up to two offline ``recall.expand_query`` names), GETs
``https://knowyourmeme.com/memes/<slug>`` and reads the entry title and its aliases (the
``<strong>`` names in the first About paragraph). KYM's ``/search`` is disallowed by robots.txt
and is never requested; only entry pages are. Requests go through ``clipforage.scrape``
(robots.txt checked, at most one request per second per host); results, misses included, are
cached 7 days in ``cache.db``. Scraping is fragile: a markup change yields no hits or a
``ProviderError``. Disabled with ``[scrape] enabled = false``.

Fuzzy slugs: with a cache, ``load_slugs`` fetches the confirmed-memes sitemap
(``SITEMAP_URL``, one ~4 MB urlset) at most once per 7 days, keeps its ``/memes/<slug>`` and
``/sensitive/memes/<slug>`` slugs in ``cache.db`` and ``fuzzy_slugs`` adds up to 3 entry slugs
that share words with the query. Sensitive slugs are fuzzy candidates only with safe search off
(``safe=False``). KYM builds the sitemap on request (~35 s), so the non-blocking path
(``block=False``, used by searches and the web UI) returns no slugs and fetches it once in a
background daemon thread. A sitemap failure is logged and only the exact slugs are tried.
"""

import difflib
import html
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from clipforage import recall, scrape
from clipforage.cache import Cache
from clipforage.models import Kind
from clipforage.providers.base import ProviderError

logger = logging.getLogger("clipforage")

PROVIDER_ID = "knowyourmeme"
ENTRY_URL = "https://knowyourmeme.com/memes/{slug}"
KYM_TTL_S = 7 * 86400
MAX_OFFLINE_CANDIDATES = 2
MAX_FUZZY_CANDIDATES = 3

SITEMAP_URL = "https://knowyourmeme.com/memes/confirmed/sitemap.xml"
SITEMAP_TTL_S = 7 * 86400
SITEMAP_FAILURE_TTL_S = 3600
"""A failed sitemap fetch is remembered for an hour so lookups do not retry it every time."""
SITEMAP_TIMEOUT_S = 90.0
"""KYM builds the sitemap on request; the first byte took ~35 s when probed (2026-10-03)."""
# Bump the version suffix whenever the slug parser changes.
SITEMAP_CACHE_KEY = f"{PROVIDER_ID}:_sitemap_v3"  # an entry slug never starts with "_"
MAX_SITEMAP_SLUGS = 50_000
MAX_SITEMAP_BYTES = 20_000_000
MAX_CHILD_SITEMAPS = 10
FUZZY_STOPWORDS = frozenset({"the", "a", "of", "and", "in", "meme"})
FUZZY_MIN_SHARED = 2
"""With 2+ key words in the query a candidate must share this many (or match ``FUZZY_MIN_RATIO``)."""
FUZZY_MIN_RATIO = 0.8
"""``SequenceMatcher`` ratio on the full slug that admits a one-shared-word candidate (typos)."""

_TITLE_RE = re.compile(r"<h1 class='content-title entry-title'>(.*?)</h1>", re.DOTALL)
_OG_TITLE_RE = re.compile(r"<meta property='og:title' content='([^']*)'")
_OG_URL_RE = re.compile(r"<meta property='og:url' content='([^']*)'")
_ABOUT_RE = re.compile(r'<h2 id="about">About</h2>\s*<p>(.*?)</p>', re.DOTALL)
_STRONG_RE = re.compile(r"<strong>(.*?)</strong>", re.DOTALL)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_SLUG_WORD_SEP_RE = re.compile(r"[-_]")
# Top-level entries only; ENTRY_URL rebuilds the loc from the slug (``_`` occurs, e.g.
# ``horse_ebooks``, never first). Excluded, per the live sitemap of 2026-10-04 (~23.4k locs):
# nested category paths (``/memes/{events,subcultures,people,sites,cultures}/<slug>``, ~5.4k:
# ENTRY_URL would build the wrong URL) and percent-encoded emoji/non-ASCII slugs (~145: the
# encoded part never matches a slugified query word). No uppercase, punctuation, trailing-slash
# or query-string locs were seen.
_SLUG_LOC_RE = re.compile(r"^https://knowyourmeme\.com/(sensitive/)?memes/([a-z0-9][a-z0-9_-]*)$")

_prefetch_lock = threading.Lock()
_prefetch_thread: threading.Thread | None = None
"""The background sitemap fetch in flight (or the last one); guarded by ``_prefetch_lock``."""


@dataclass(frozen=True)
class SitemapSlugs:
    """Entry slugs from the sitemap: ``/memes/<slug>`` and ``/sensitive/memes/<slug>``."""

    memes: list[str] = field(default_factory=list)
    sensitive: list[str] = field(default_factory=list)

    def candidates(self, *, safe: bool) -> list[str]:
        """Fuzzy candidates: ``memes`` with safe search on, ``memes`` + ``sensitive`` when off."""
        return list(self.memes) if safe else [*self.memes, *self.sensitive]


@dataclass(frozen=True)
class RecallHit:
    """A Know Your Meme entry: its title, the aliases it lists, and the entry URL."""

    name: str
    aliases: list[str] = field(default_factory=list)
    source_url: str = ""


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def parse_entry(page: str, url: str) -> RecallHit | None:
    """Title + aliases of a KYM entry page; None when the page has no recognisable title."""
    title = _TITLE_RE.search(page)
    name = _text(title.group(1)) if title else ""
    if not name:
        og = _OG_TITLE_RE.search(page)
        name = _text(og.group(1)).removesuffix("| Know Your Meme").strip() if og else ""
    if not name:
        return None
    aliases: list[str] = []
    about = _ABOUT_RE.search(page)
    if about:
        seen = {name.lower()}
        for strong in _STRONG_RE.findall(about.group(1)):
            alias = _text(strong)
            if alias and alias.lower() not in seen:
                seen.add(alias.lower())
                aliases.append(alias)
    canonical = _OG_URL_RE.search(page)
    source_url = html.unescape(canonical.group(1)) if canonical else url
    return RecallHit(name=name, aliases=aliases, source_url=source_url)


def _cache_key(slug: str) -> str:
    return f"{PROVIDER_ID}:{slug}"


def _cached(cache: Cache | None, slug: str) -> tuple[bool, RecallHit | None]:
    """``(found, hit)``; a cached miss is ``(True, None)``."""
    if cache is None:
        return False, None
    try:
        payload = cache.get_blob(_cache_key(slug))
    except (sqlite3.Error, OSError) as exc:
        logger.warning("knowyourmeme cache unavailable: %s", exc)
        return False, None
    if payload is None:
        return False, None
    data = json.loads(payload)
    if not data:
        return True, None
    return True, RecallHit(
        name=str(data["name"]),
        aliases=[str(a) for a in data.get("aliases", [])],
        source_url=str(data.get("source_url", "")),
    )


def _store(cache: Cache | None, slug: str, hit: RecallHit | None) -> None:
    if cache is None:
        return
    payload = (
        json.dumps({"name": hit.name, "aliases": hit.aliases, "source_url": hit.source_url})
        if hit
        else "null"
    )
    try:
        cache.set_blob(_cache_key(slug), payload, KYM_TTL_S)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("knowyourmeme cache unavailable: %s", exc)


def _fetch_entry(
    slug: str, client: httpx.Client | None, throttle: scrape.HostThrottle | None
) -> RecallHit | None:
    url = ENTRY_URL.format(slug=slug)
    response = scrape.request(
        "GET",
        url,
        provider=PROVIDER_ID,
        client=client,
        throttle=throttle,
        headers={"Accept": scrape.ACCEPT_HTML},
    )
    if response.status_code in (404, 410):
        return None
    if response.status_code != 200:
        raise scrape.ScrapeHTTPError(
            PROVIDER_ID, f"HTTP {response.status_code} for {url}", response.status_code
        )
    return parse_entry(response.text, str(response.url))


def _fetch_sitemap(
    url: str, client: httpx.Client | None, throttle: scrape.HostThrottle | None
) -> str:
    response = scrape.request(
        "GET",
        url,
        provider=PROVIDER_ID,
        client=client,
        throttle=throttle,
        headers={"Accept": "application/xml"},
        timeout=SITEMAP_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise scrape.ScrapeHTTPError(
            PROVIDER_ID, f"HTTP {response.status_code} for {url}", response.status_code
        )
    return response.text


def _collect_slugs(
    client: httpx.Client | None, throttle: scrape.HostThrottle | None
) -> SitemapSlugs:
    """Slugs from ``SITEMAP_URL`` (and its children if it is an index), within the caps."""
    slugs: dict[str, None] = {}
    sensitive: dict[str, None] = {}
    budget = MAX_SITEMAP_BYTES
    queue = [SITEMAP_URL]
    children = 0
    while queue and budget > 0 and len(slugs) + len(sensitive) < MAX_SITEMAP_SLUGS:
        text = _fetch_sitemap(queue.pop(0), client, throttle)[:budget]
        budget -= len(text)
        locs = [html.unescape(loc) for loc in _LOC_RE.findall(text)]
        if "<sitemapindex" in text[:2000]:
            for loc in locs:
                if children < MAX_CHILD_SITEMAPS and loc.startswith(SITEMAP_URL.rsplit("/", 1)[0]):
                    queue.append(loc)
                    children += 1
            continue
        for loc in locs:
            match = _SLUG_LOC_RE.match(loc)
            if match:
                (sensitive if match.group(1) else slugs)[match.group(2)] = None
                if len(slugs) + len(sensitive) >= MAX_SITEMAP_SLUGS:
                    break
    return SitemapSlugs(memes=list(slugs), sensitive=list(sensitive))


def _decode_sitemap(payload: str) -> SitemapSlugs | None:
    """The cached ``{"memes": [...], "sensitive": [...]}``; None for any other shape."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None  # e.g. a stale bare list: refetch so the sensitive slugs get collected
    return SitemapSlugs(
        memes=[str(s) for s in data.get("memes", [])],
        sensitive=[str(s) for s in data.get("sensitive", [])],
    )


def _fetch_and_store(
    cache: Cache, client: httpx.Client | None, throttle: scrape.HostThrottle | None
) -> SitemapSlugs:
    try:
        slugs = _collect_slugs(client, throttle)
    except ProviderError as exc:
        logger.warning("knowyourmeme sitemap unavailable, exact slugs only: %s", exc)
        slugs, ttl = SitemapSlugs(), SITEMAP_FAILURE_TTL_S
    else:
        logger.info(
            "knowyourmeme sitemap: %d slugs (+%d sensitive) fetched",
            len(slugs.memes),
            len(slugs.sensitive),
        )
        ttl = SITEMAP_TTL_S if slugs.memes or slugs.sensitive else SITEMAP_FAILURE_TTL_S
    payload = json.dumps({"memes": slugs.memes, "sensitive": slugs.sensitive})
    try:
        cache.set_blob(SITEMAP_CACHE_KEY, payload, ttl)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("knowyourmeme cache unavailable: %s", exc)
    return slugs


def _prefetch(
    path: Path, client: httpx.Client | None, throttle: scrape.HostThrottle | None
) -> None:
    try:
        with Cache(path) as cache:  # its own connection: sqlite3 objects stay in their thread
            _fetch_and_store(cache, client, throttle)
    except Exception as exc:  # noqa: BLE001 - a background prefetch must never raise
        logger.warning("knowyourmeme sitemap prefetch failed: %s", exc)


def _start_prefetch(
    path: Path, client: httpx.Client | None, throttle: scrape.HostThrottle | None
) -> None:
    """Start the background sitemap fetch unless one is already in flight."""
    global _prefetch_thread
    with _prefetch_lock:
        if _prefetch_thread is not None and _prefetch_thread.is_alive():
            return
        _prefetch_thread = threading.Thread(
            target=_prefetch,
            args=(path, client, throttle),
            name="clipforage-kym-sitemap",
            daemon=True,
        )
        _prefetch_thread.start()


def prefetch_thread() -> threading.Thread | None:
    """The background sitemap fetch thread, if one was started (test hook)."""
    with _prefetch_lock:
        return _prefetch_thread


def prefetch_running() -> bool:
    """True while a background sitemap fetch (``block=False`` cache miss) is still running."""
    thread = prefetch_thread()
    return thread is not None and thread.is_alive()


def reset_prefetch(timeout: float | None = 30.0) -> None:
    """Join any background sitemap fetch (up to ``timeout``) and forget it (used by tests)."""
    global _prefetch_thread
    thread = prefetch_thread()
    if thread is not None:
        thread.join(timeout)
    with _prefetch_lock:
        _prefetch_thread = None


def load_slugs(
    cache: Cache,
    client: httpx.Client | None = None,
    throttle: scrape.HostThrottle | None = None,
    *,
    block: bool,
    on_fetch: Callable[[], None] | None = None,
) -> SitemapSlugs:
    """Entry slugs from the confirmed-memes sitemap, cached 7 days; empty on any failure.

    On a cache miss, ``block=True`` fetches synchronously (calling ``on_fetch`` first, e.g. to
    print a notice); ``block=False`` starts one background daemon fetch (``prefetch_thread``)
    that stores the result, and returns empty slugs at once.
    """
    try:
        payload = cache.get_blob(SITEMAP_CACHE_KEY)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("knowyourmeme cache unavailable: %s", exc)
        return SitemapSlugs()
    if payload is not None:
        cached = _decode_sitemap(payload)
        if cached is not None:
            logger.debug("knowyourmeme sitemap: cached")
            return cached
    if not block:
        _start_prefetch(cache.path, client, throttle)
        return SitemapSlugs()
    if on_fetch is not None:
        on_fetch()
    return _fetch_and_store(cache, client, throttle)


def fuzzy_slugs(
    query: str, slugs: list[str], *, limit: int = MAX_FUZZY_CANDIDATES, exclude: Iterable[str] = ()
) -> list[str]:
    """Up to ``limit`` slugs sharing the most words with ``query`` (ties: closest spelling).

    Key words are the query's words of 3+ letters that are not stopwords (``FUZZY_STOPWORDS``).
    A candidate shares at least one; with 2+ key words it must share ``FUZZY_MIN_SHARED`` or be
    a near spelling of the whole query (``SequenceMatcher`` ratio >= ``FUZZY_MIN_RATIO``, e.g.
    ``distracted-boyfriend`` for "distracted boyfreind"). Slugs in ``exclude`` (the exact
    candidates) are skipped. Slug words split on ``-`` and ``_`` (``horse_ebooks``); the slug
    itself is returned unchanged.
    """
    target = scrape.slugify(query)
    key_words = {t for t in target.split("-") if len(t) >= 3 and t not in FUZZY_STOPWORDS}
    if not key_words or limit <= 0:
        return []
    need = min(FUZZY_MIN_SHARED, len(key_words))
    skip = set(exclude)
    matcher = difflib.SequenceMatcher(b=target, autojunk=False)
    scored: list[tuple[int, str]] = []
    for slug in slugs:
        shared = len(key_words.intersection(_SLUG_WORD_SEP_RE.split(slug)))
        if not shared or slug in skip:
            continue
        if shared < need:
            matcher.set_seq1(slug)
            if not (
                matcher.real_quick_ratio() >= FUZZY_MIN_RATIO
                and matcher.quick_ratio() >= FUZZY_MIN_RATIO
                and matcher.ratio() >= FUZZY_MIN_RATIO
            ):
                continue
        scored.append((shared, slug))
    if not scored:
        return []
    scored.sort(key=lambda item: item[0], reverse=True)
    floor = scored[min(limit, len(scored)) - 1][0]
    ranked: list[tuple[int, float, str]] = []
    for shared, slug in scored:
        if shared < floor:
            break
        matcher.set_seq1(slug)
        ranked.append((shared, matcher.ratio(), slug))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [slug for _, _, slug in ranked[:limit]]


def kym_lookup(
    query: str,
    kind: Kind | None = None,
    *,
    cache: Cache | None = None,
    client: httpx.Client | None = None,
    throttle: scrape.HostThrottle | None = None,
    safe: bool = True,
    block_sitemap: bool = False,
    on_sitemap_fetch: Callable[[], None] | None = None,
) -> list[RecallHit]:
    """Know Your Meme entries for ``query``, up to 2 offline recall names and, with a ``cache``,
    up to 3 fuzzy sitemap slugs (``fuzzy_slugs``); deduped by URL.

    Fuzzy candidates include ``/sensitive/`` entries only when ``safe`` is False (exact slugs
    are tried either way). ``block_sitemap`` / ``on_sitemap_fetch`` are ``load_slugs``'s
    ``block`` / ``on_fetch``: by default a missing sitemap is fetched in the background and
    this lookup gets no fuzzy slugs.

    Raises ``ProviderError`` (provider ``knowyourmeme``) when scraping is disabled, robots.txt
    disallows the page, or a request fails; a 404 is simply no hit.
    """
    if not scrape.enabled():
        raise ProviderError(PROVIDER_ID, scrape.DISABLED_MESSAGE)
    candidates = [query, *recall.expand_query(query, kind, max_expansions=MAX_OFFLINE_CANDIDATES)]
    slugs = [s for s in dict.fromkeys(scrape.slugify(c) for c in candidates) if s]
    if cache is not None:
        known = load_slugs(
            cache, client, throttle, block=block_sitemap, on_fetch=on_sitemap_fetch
        ).candidates(safe=safe)
        slugs += fuzzy_slugs(query, known, limit=MAX_FUZZY_CANDIDATES, exclude=slugs)
    hits: list[RecallHit] = []
    seen_urls: set[str] = set()
    for slug in slugs:
        found, hit = _cached(cache, slug)
        if not found:
            hit = _fetch_entry(slug, client, throttle)
            _store(cache, slug, hit)
        if hit is not None and hit.source_url not in seen_urls:
            seen_urls.add(hit.source_url)
            hits.append(hit)
    return hits


def hit_names(hits: list[RecallHit], query: str, *, limit: int) -> list[str]:
    """Up to ``limit`` distinct names (titles, then aliases) that differ from ``query``."""
    skip = {recall.normalize(query)}
    names: list[str] = []
    for candidate in [h.name for h in hits] + [a for h in hits for a in h.aliases]:
        key = recall.normalize(candidate)
        if key and key not in skip:
            skip.add(key)
            names.append(candidate)
        if len(names) >= limit:
            break
    return names
