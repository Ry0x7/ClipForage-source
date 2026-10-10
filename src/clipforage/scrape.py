"""Throttled, robots.txt-aware fetching for the allowlisted scraping sources.

Some free sources have no public API (Mixkit, LottieFiles' undocumented GraphQL endpoint, Know
Your Meme entry pages). Fetching them is a fragile capability: the markup or endpoint can change
or be gated at any time, and then the provider fails (``ProviderError``) and is skipped while
the rest of the search continues. Every such request goes through this module, which:

- sends the honest project User-Agent (``http.USER_AGENT``) via the shared ``httpx`` client;
- checks ``/robots.txt`` (fetched once per host per process) before every request and refuses
  disallowed paths;
- spaces requests to the same host at least ``min_interval_s()`` apart (default 1 s);
- never paginates: callers fetch exactly the one page the user's query maps to.

Search results are cached 24h by the registry (providers set ``SEARCH_TTL_S = 86400``).
Turn the whole capability off with ``[scrape] enabled = false`` in config.toml or
``CLIPFORAGE_SCRAPE_ENABLED=0``; scraping providers are then skipped with a clear message.
"""

import logging
import re
import threading
import time
import urllib.robotparser
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from clipforage import config, http
from clipforage.providers.base import ProviderError

logger = logging.getLogger("clipforage")

DEFAULT_MIN_INTERVAL_S = 1.0
"""Default (and minimum) spacing between two requests to the same host, in seconds."""
TIMEOUT_S = 15.0
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
DISABLED_MESSAGE = (
    "scraping is disabled; set [scrape] enabled = true in config.toml "
    "or CLIPFORAGE_SCRAPE_ENABLED=1 to search this source"
)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(query: str) -> str:
    """Lowercase, each run of non-alphanumerics -> ``-``, trimmed (``"Cat  Jump!"`` -> ``cat-jump``)."""
    return _SLUG_RE.sub("-", query.lower()).strip("-")


def enabled() -> bool:
    """True unless ``[scrape] enabled = false`` / ``CLIPFORAGE_SCRAPE_ENABLED=0`` is set."""
    return config.get_bool_setting("scrape", "enabled", env="SCRAPE_ENABLED", default=True)


def min_interval_s() -> float:
    """``[scrape] min_interval_s`` (env ``CLIPFORAGE_SCRAPE_MIN_INTERVAL_S``), at least 1.0."""
    raw = config.get_setting("scrape", "min_interval_s", env="SCRAPE_MIN_INTERVAL_S")
    try:
        value = float(raw) if raw is not None else DEFAULT_MIN_INTERVAL_S
    except ValueError:
        value = DEFAULT_MIN_INTERVAL_S
    return max(value, DEFAULT_MIN_INTERVAL_S)


class HostThrottle:
    """Per-host minimum interval between requests; thread-safe.

    ``wait(host)`` reserves the next free slot for ``host`` under a lock, then sleeps (outside
    the lock) until it. ``interval_s`` None reads ``min_interval_s()`` on every call.
    """

    def __init__(
        self,
        interval_s: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval_s = interval_s
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def interval_s(self) -> float:
        return self._interval_s if self._interval_s is not None else min_interval_s()

    def wait(self, host: str) -> float:
        """Block until ``host`` may be requested again; return the seconds slept."""
        interval = self.interval_s
        with self._lock:
            now = self._clock()
            start = max(now, self._next.get(host, now))
            self._next[host] = start + interval
        delay = start - now
        if delay > 0:
            self._sleep(delay)
        return delay

    def reset(self) -> None:
        """Forget all hosts (used by tests)."""
        with self._lock:
            self._next.clear()


THROTTLE = HostThrottle()
"""The process-wide throttle every scraping request goes through."""

_robots: dict[str, urllib.robotparser.RobotFileParser] = {}
_robots_lock = threading.Lock()


def reset() -> None:
    """Drop the robots.txt cache and throttle state (used by tests)."""
    with _robots_lock:
        _robots.clear()
    THROTTLE.reset()


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not an http(s) URL: {url!r}")
    return f"{parts.scheme}://{parts.netloc}", parts.netloc.lower()


def _client(client: httpx.Client | None) -> httpx.Client:
    return client if client is not None else http.get_client()


def _load_robots(
    origin: str, host: str, client: httpx.Client | None, throttle: HostThrottle
) -> urllib.robotparser.RobotFileParser:
    parser = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
    try:
        throttle.wait(host)
        response = _client(client).get(origin + "/robots.txt", timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        logger.warning("robots.txt for %s could not be fetched (%s); assuming allowed", host, exc)
        parser.allow_all = True
        return parser
    status = response.status_code
    if status == 200:
        parser.parse(response.text.splitlines())
    elif status in (401, 403):
        parser.disallow_all = True  # same convention as urllib.robotparser.read()
    elif 400 <= status < 500:
        parser.allow_all = True  # no robots.txt: everything allowed
    else:
        logger.warning("robots.txt for %s returned HTTP %s; assuming allowed", host, status)
        parser.allow_all = True
    return parser


def robots_allows(
    url: str,
    user_agent: str = http.USER_AGENT,
    *,
    client: httpx.Client | None = None,
    throttle: HostThrottle | None = None,
) -> bool:
    """True if ``url``'s host robots.txt lets ``user_agent`` fetch it.

    robots.txt is fetched once per host per process (cached); if it cannot be fetched the URL
    is assumed allowed and a warning is logged.
    """
    origin, host = _origin(url)
    with _robots_lock:
        parser = _robots.get(origin)
    if parser is None:
        parser = _load_robots(origin, host, client, throttle or THROTTLE)
        with _robots_lock:
            parser = _robots.setdefault(origin, parser)
    return parser.can_fetch(user_agent, url)


class ScrapeHTTPError(ProviderError):
    """A scraping request answered with an unexpected HTTP status (``.status``)."""

    def __init__(self, provider: str, message: str, status: int) -> None:
        super().__init__(provider, message)
        self.status = status


MAX_REDIRECTS = 5


def request(
    method: str,
    url: str,
    *,
    provider: str,
    client: httpx.Client | None = None,
    throttle: HostThrottle | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = TIMEOUT_S,
    **kwargs: Any,
) -> httpx.Response:
    """Robots-check, throttle, then send one request; return the response (any status).

    GET / HEAD redirects are followed by hand (up to ``MAX_REDIRECTS``) so every hop is
    robots-checked and throttled too. Raises ``ProviderError`` (as ``provider``) when robots.txt
    disallows a URL, there are too many redirects, or the request fails at the transport level.
    """
    throttle = throttle or THROTTLE
    follow = method.upper() in ("GET", "HEAD")
    for _ in range(MAX_REDIRECTS + 1):
        if not robots_allows(url, client=client, throttle=throttle):
            raise ProviderError(provider, f"robots.txt disallows {urlsplit(url).path or '/'}")
        _, host = _origin(url)
        throttle.wait(host)
        logger.debug("scrape %s %s", method, url)
        try:
            response = _client(client).request(
                method, url, headers=headers, timeout=timeout, follow_redirects=False, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ProviderError(provider, f"{type(exc).__name__}: {exc}") from None
        location = response.headers.get("location")
        if not (follow and response.is_redirect and location):
            return response
        url = urljoin(url, location)
    raise ProviderError(provider, f"too many redirects for {url}")


def fetch_html(
    url: str,
    *,
    provider: str = "scrape",
    client: httpx.Client | None = None,
    throttle: HostThrottle | None = None,
) -> str:
    """GET an HTML page (robots-checked, throttled); status != 200 raises ``ScrapeHTTPError``."""
    response = request(
        "GET",
        url,
        provider=provider,
        client=client,
        throttle=throttle,
        headers={"Accept": ACCEPT_HTML},
    )
    if response.status_code != 200:
        raise ScrapeHTTPError(
            provider, f"HTTP {response.status_code} for {url}", response.status_code
        )
    return response.text
