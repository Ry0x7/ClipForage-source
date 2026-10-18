"""In-repo provider registry and the fan-out search across providers."""

import importlib.metadata
import logging
import sqlite3
from collections.abc import Callable, Iterable

from pydantic import ValidationError

from clipforage import safesearch, scrape
from clipforage.cache import DEFAULT_TTL_S, Cache
from clipforage.config import config_path, get_setting, has_key
from clipforage.models import SHORTS_MAX_DURATION_S, Asset, Kind, SearchFilters
from clipforage.providers.base import Provider, ProviderError

PLUGIN_GROUP = "clipforage.providers"

logger = logging.getLogger("clipforage")

_PROVIDERS: dict[str, Provider] = {}
_SEEN_PLUGINS: set[str] = set()


def register(provider: Provider) -> Provider:
    """Register a provider instance by its id. Raises ValueError on duplicate id."""
    if provider.id in _PROVIDERS:
        raise ValueError(f"provider {provider.id!r} is already registered")
    _PROVIDERS[provider.id] = provider
    return provider


def get(provider_id: str) -> Provider:
    """Return the provider with this id. Raises KeyError if unknown."""
    try:
        return _PROVIDERS[provider_id]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS)) or "none"
        raise KeyError(f"unknown provider {provider_id!r}; registered: {known}") from None


def all_providers() -> list[Provider]:
    """Return all registered providers sorted by id."""
    return [_PROVIDERS[pid] for pid in sorted(_PROVIDERS)]


def providers_for(kind: Kind) -> list[Provider]:
    """Return registered providers that support `kind`, sorted by id."""
    return [p for p in all_providers() if kind in p.kinds]


def _passes_filters(asset: Asset, filters: SearchFilters) -> bool:
    if filters.commercial_only and asset.commercial_ok is not True:
        return False
    if filters.shorts and Kind.VIDEO in (filters.kind, asset.kind):
        # Unknown length is dropped on purpose: a short cannot be confirmed without one.
        if asset.duration_s is None or asset.duration_s > SHORTS_MAX_DURATION_S:
            return False
    if asset.duration_s is not None:
        if filters.min_duration is not None and asset.duration_s < filters.min_duration:
            return False
        if filters.max_duration is not None and asset.duration_s > filters.max_duration:
            return False
    return (
        filters.orientation is None
        or asset.orientation is None
        or asset.orientation == filters.orientation
    )


def search_all(
    query: str,
    filters: SearchFilters,
    providers: Iterable[Provider] | None = None,
    *,
    cache: Cache | None = None,
    use_cache: bool = True,
    dropped: list[Asset] | None = None,
) -> tuple[list[Asset], list[ProviderError]]:
    """Search every provider; one provider failing never fails the whole search.

    With a ``cache``, raw provider results are looked up (unless ``use_cache`` is False)
    and stored with the provider's ``SEARCH_TTL_S`` (default ``DEFAULT_TTL_S``); an empty
    result is never stored. Cache failures are reported as ``cache:`` errors and fall through
    to a live search. With ``filters.safe``, explicit results are dropped (see ``search_each``).
    """
    per_provider, errors = search_each(
        query, filters, providers, cache=cache, use_cache=use_cache, dropped=dropped
    )
    return [a for assets in per_provider for a in assets], errors


def search_each(
    query: str,
    filters: SearchFilters,
    providers: Iterable[Provider] | None = None,
    *,
    cache: Cache | None = None,
    use_cache: bool = True,
    dropped: list[Asset] | None = None,
) -> tuple[list[list[Asset]], list[ProviderError]]:
    """Like ``search_all`` but return one filtered result list per provider, in target order.

    Providers with ``scrapes = True`` are skipped with an error while ``scrape.enabled()`` is
    false, and keyed providers without a key are skipped likewise. With ``filters.safe``,
    results ``safesearch.is_explicit`` flags are removed after the filters (the cache keeps the
    raw results) and appended to ``dropped`` when given.
    """
    targets = providers_for(filters.kind) if providers is None else list(providers)
    per_provider: list[list[Asset]] = []
    errors: list[ProviderError] = []
    for p in targets:
        assets: list[Asset] = []
        per_provider.append(assets)
        try:
            if getattr(p, "scrapes", False) and not scrape.enabled():
                errors.append(ProviderError(p.id, scrape.DISABLED_MESSAGE))
                continue
            if p.requires_key and not has_key(p.id):
                errors.append(
                    ProviderError(
                        p.id,
                        f"no API key; set CLIPFORAGE_{p.id.upper()}_KEY "
                        f"or add it to {config_path()}",
                    )
                )
                continue
            cache_empty = bool(getattr(p, "cache_empty_results", False))
            if cache is not None and use_cache:
                try:
                    hits = cache.get_search(p.id, query, filters, allow_empty=cache_empty)
                except (sqlite3.Error, OSError, ValidationError) as e:
                    errors.append(ProviderError(p.id, f"cache: {e}"))
                    hits = None
            else:
                hits = None
            if hits is not None:
                # Cached raw results: fall through so the filters below still apply.
                assets.extend(hits)
            else:
                results = p.search(query, filters.kind, filters)
                assets.extend(results)
                # Don't cache an empty result unless the provider opts in
                # (``cache_empty_results``): it is often a transient failure (e.g. a yt-dlp
                # hiccup) and would hide the provider for its whole TTL. Stable APIs (and
                # Pixabay's 24h caching terms) opt in.
                if cache is not None and (results or cache_empty):
                    try:
                        ttl_s = getattr(p, "SEARCH_TTL_S", DEFAULT_TTL_S)
                        cache.put_search(p.id, query, filters, results, ttl_s=ttl_s)
                    except (sqlite3.Error, OSError, ValidationError) as e:
                        errors.append(ProviderError(p.id, f"cache: {e}"))
        except ProviderError as e:
            errors.append(e)
        except Exception as e:  # noqa: BLE001 - isolate provider failures
            errors.append(ProviderError(p.id, f"{type(e).__name__}: {e}"))
        assets[:] = [a for a in assets if _passes_filters(a, filters)]
        if filters.safe:
            kept: list[Asset] = []
            for asset in assets:
                if not safesearch.is_explicit(asset):
                    kept.append(asset)
                elif dropped is not None:
                    dropped.append(asset)
            assets[:] = kept
    return per_provider, errors


def load_builtins() -> None:
    """Register the built-in providers, then entry point plugins, not registered yet (idempotent)."""
    from clipforage.download.service import parse_cookies_from_browser
    from clipforage.providers.freesound import FreesoundProvider
    from clipforage.providers.google_fonts import GoogleFontsProvider
    from clipforage.providers.internet_archive import InternetArchiveProvider
    from clipforage.providers.lottiefiles import LottieFilesProvider
    from clipforage.providers.mixkit import MixkitProvider
    from clipforage.providers.openverse import OpenverseProvider
    from clipforage.providers.pexels import PexelsProvider
    from clipforage.providers.pixabay import PixabayProvider
    from clipforage.providers.wikimedia import WikimediaProvider
    from clipforage.providers.youtube import YouTubeProvider

    if YouTubeProvider.id not in _PROVIDERS:
        spec = get_setting("download", "cookies_from_browser", env="COOKIES_FROM_BROWSER")
        cookies: tuple[str | None, ...] | None = None
        if spec:
            try:
                cookies = parse_cookies_from_browser(spec)
            except ValueError:
                cookies = None  # a bad setting must not break search; `get` reports it
        register(YouTubeProvider(cookies_from_browser=cookies))
    if PexelsProvider.id not in _PROVIDERS:
        register(PexelsProvider())
    if PixabayProvider.id not in _PROVIDERS:
        register(PixabayProvider())
    if OpenverseProvider.id not in _PROVIDERS:
        register(OpenverseProvider())
    if WikimediaProvider.id not in _PROVIDERS:
        register(WikimediaProvider())
    if FreesoundProvider.id not in _PROVIDERS:
        register(FreesoundProvider())
    if InternetArchiveProvider.id not in _PROVIDERS:
        register(InternetArchiveProvider())
    if GoogleFontsProvider.id not in _PROVIDERS:
        register(GoogleFontsProvider())
    if MixkitProvider.id not in _PROVIDERS:
        register(MixkitProvider())
    if LottieFilesProvider.id not in _PROVIDERS:
        register(LottieFilesProvider())
    load_plugins()  # after the built-ins, so a built-in id wins over a plugin's


def _plugin_provider(obj: object) -> Provider:
    """Turn an entry point's loaded object (instance, class or factory) into a Provider."""
    if isinstance(obj, type) or (callable(obj) and not isinstance(obj, Provider)):
        obj = obj()
    if not isinstance(obj, Provider):
        raise TypeError(f"{type(obj).__name__} is not a Provider")
    return obj


def load_plugins(*, warn: Callable[[str], None] | None = None) -> list[str]:
    """Register third-party providers from the ``clipforage.providers`` entry point group.

    Each entry point may load a ``Provider`` instance, a Provider class, or a zero-arg
    factory returning one. A failing plugin (import error, factory exception, non-Provider
    object, duplicate id) is reported to ``warn`` (default: the ``clipforage`` logger) as
    ``plugin <name>: <reason>`` and skipped. Entry point names already processed are skipped,
    so repeated calls are idempotent.
    Returns the ids registered by this call.
    """
    report = warn or logger.warning
    registered: list[str] = []
    eps = importlib.metadata.entry_points(group=PLUGIN_GROUP)
    for ep in eps:
        if ep.name in _SEEN_PLUGINS:
            continue
        _SEEN_PLUGINS.add(ep.name)
        try:
            provider = _plugin_provider(ep.load())
            if provider.id in _PROVIDERS:
                raise ValueError(f"duplicate provider id {provider.id!r}")
            register(provider)
        except Exception as e:  # noqa: BLE001 - isolate plugin failures
            report(f"plugin {ep.name}: {type(e).__name__}: {e}")
            continue
        registered.append(provider.id)
    return registered


def clear() -> None:
    """Remove all registered providers (for tests)."""
    _PROVIDERS.clear()
    _SEEN_PLUGINS.clear()
