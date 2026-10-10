"""Typer-free service layer: search, download, history and providers for the CLI and web API.

Functions raise exceptions instead of printing; frontends map them to output / exit codes /
HTTP statuses. ``RefError`` and ``CacheUnavailableError`` carry user-facing messages.
"""

import json
import logging
import math
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipforage import recall, recall_online, relevance, rights, safesearch, scrape
from clipforage.cache import Cache
from clipforage.config import get_setting, has_key
from clipforage.download import service as dl
from clipforage.download.service import (
    KEY_BLEND,
    ProgressHook,
    chroma_similarity,
    download_plan,
    download_url,
)
from clipforage.download.sidecar import sidecar_path, write_sidecar
from clipforage.models import (
    QUALITIES,
    Asset,
    DownloadResult,
    Kind,
    PhraseHit,
    PhraseOutcome,
    SearchFilters,
    SearchOutcome,
    Style,
)
from clipforage.providers import registry
from clipforage.providers.base import Provider, ProviderError

logger = logging.getLogger("clipforage")

Warn = Callable[[str], None]

REF_RE = re.compile(r"^[a-z_]+:.+$")

_SIDECAR_BASE_KEYS: frozenset[str] = frozenset(
    {
        "license",
        "attribution_text",
        "source_url",
        "provider",
        "fetched_at",
        "asset_id",
        "title",
        "kind",
        "commercial_ok",
    }
)


class RefError(ValueError):
    """A bad, unknown or uncached ``<provider>:<id>`` ref (or an option it cannot take).

    ``str(exc)`` is the full user-facing error; ``.hint`` is a short fix suggestion.
    """

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


class CacheUnavailableError(RuntimeError):
    """cache.db could not be opened or read; ``str(exc)`` is ``cache unavailable: ...``."""


def _known_providers() -> list[str]:
    return sorted(p.id for p in registry.all_providers())


def _bad_ref(ref: str) -> RefError:
    known = ", ".join(_known_providers())
    return RefError(
        f"{ref!r} is not a URL (http:// or https://) or a <provider>:<id> ref; providers: {known}",
        hint=f"use a <provider>:<id> ref from search output; providers: {known}",
    )


def parse_ref(ref: str) -> tuple[str, str]:
    """Split ``<provider>:<id>`` into ``(provider_id, asset_id)``. Raises ``RefError``."""
    if not REF_RE.match(ref):
        raise _bad_ref(ref)
    provider_id, _, asset_id = ref.partition(":")
    return provider_id, asset_id


def list_providers() -> list[dict[str, Any]]:
    """Return every built-in and plugin provider as ``{id, kinds, requires_key, has_key}``, sorted by id."""
    registry.load_builtins()
    return [
        {
            "id": p.id,
            "kinds": sorted(k.value for k in p.kinds),
            "requires_key": p.requires_key,
            "has_key": has_key(p.id),
        }
        for p in registry.all_providers()
    ]


_STYLE_WEIGHTS: dict[str, dict[str, int]] = {
    "any": {"internet_archive": 0},
    "meme": {
        "youtube": 3,
        "pexels": 2,
        "pixabay": 2,
        "openverse": 1,
        "wikimedia": 0,
        "internet_archive": 0,
    },
    "cinematic": {
        "pexels": 3,
        "pixabay": 3,
        "youtube": 2,
        "openverse": 1,
        "wikimedia": 1,
        "internet_archive": 1,
    },
}
_STYLE_WEIGHTS["green_screen"] = _STYLE_WEIGHTS["meme"]
# Audio kinds rank by kind, not style: cleared sources first, YouTube (Content ID risk) after.
_KIND_WEIGHTS: dict[Kind, dict[str, int]] = {
    Kind.AUDIO_SFX: {
        "freesound": 3,
        "openverse": 2,
        "youtube": 2,
        "internet_archive": 1,
        "wikimedia": 1,
    },
    Kind.AUDIO_MUSIC: {
        "freesound": 2,
        "youtube": 2,
        "openverse": 1,
        "internet_archive": 1,
        "wikimedia": 1,
    },
}


def provider_weight(provider_id: str, style: Style, kind: Kind | None = None) -> int:
    """Return how many items ``provider_id`` contributes per merge round for ``style``.

    Audio kinds use their own table (``_KIND_WEIGHTS``) whatever the style. ``0`` demotes the
    provider: its results go after all weighted results. Unlisted providers get ``1``; style
    ``any`` gives every provider ``1`` except ``internet_archive`` (``0``).
    """
    if kind is not None and kind in _KIND_WEIGHTS:
        return _KIND_WEIGHTS[kind].get(provider_id, 1)
    return _STYLE_WEIGHTS.get(style, {}).get(provider_id, 1)


def _round_robin[T](lists: Sequence[Sequence[T]], weights: Sequence[int]) -> list[T]:
    out: list[T] = []
    pos = [0] * len(lists)
    while True:
        progressed = False
        for i, items in enumerate(lists):
            take = items[pos[i] : pos[i] + weights[i]]
            if take:
                out.extend(take)
                pos[i] += len(take)
                progressed = True
        if not progressed:
            return out


def interleave_weighted[T](lists: Sequence[Sequence[T]], weights: Sequence[int]) -> list[T]:
    """Merge lists in rounds where list ``i`` contributes up to ``weights[i]`` items per round.

    Within a round, heavier lists go first (stable: equal weights keep their input order).
    Lists with weight ``0`` are demoted: they are merged round-robin among themselves and
    appended after every weighted item. Each list's internal order is preserved.
    """
    if len(lists) != len(weights):
        raise ValueError("lists and weights must have the same length")
    if any(w < 0 for w in weights):
        raise ValueError("weights must be >= 0")
    weighted = sorted(
        ((items, w) for items, w in zip(lists, weights, strict=True) if w > 0),
        key=lambda pair: -pair[1],
    )
    demoted = [items for items, w in zip(lists, weights, strict=True) if w == 0]
    head = _round_robin([items for items, _ in weighted], [w for _, w in weighted])
    return head + _round_robin(demoted, [1] * len(demoted))


def interleave[T](lists: Sequence[Sequence[T]]) -> list[T]:
    """Merge lists round-robin: 1st of each, then 2nd of each, ...; exhausted lists are skipped."""
    return interleave_weighted(lists, [1] * len(lists))


def _order_for_shorts(assets: Sequence[Asset], style: Style) -> list[Asset]:
    """Order a merged ``--shorts`` video result: YouTube first, then portrait stock first.

    YouTube orientation is unknown: yt-dlp's flat search entries carry no width/height and
    never use ``/shorts/`` URLs, so a portrait-first sort across providers pushed (often
    off-topic) stock clips above every YouTube hit. Non-YouTube results keep a stable
    portrait-first sort among themselves. ``cinematic`` skips the YouTube grouping (stock
    b-roll is as relevant there) and keeps only the stable portrait-first sort.
    """
    if style == "cinematic":
        return sorted(assets, key=lambda a: a.orientation != "portrait")
    youtube = [a for a in assets if a.provider == "youtube"]
    rest = [a for a in assets if a.provider != "youtube"]
    return youtube + sorted(rest, key=lambda a: a.orientation != "portrait")


def search(
    query: str,
    filters: SearchFilters,
    *,
    provider: str | None = None,
    use_cache: bool = True,
    limit: int | None = None,
    warn: Warn | None = None,
    cleared_only: bool = False,
    dropped: list[Asset] | None = None,
) -> tuple[list[Asset], list[ProviderError]]:
    """Search all providers (or only ``provider``); return ``(assets, per-provider errors)``.

    ``cleared_only`` drops assets whose rights level is not cleared (``rights.is_cleared``)
    before the result is cut to ``limit``. Results hidden by safe search (``filters.safe``)
    are appended to ``dropped`` when given.

    Raises ``LookupError`` for an unknown provider id. An unavailable cache is not fatal: the
    search runs uncached and ``warn`` (default: the ``clipforage`` logger) gets a message.
    """
    registry.load_builtins()
    if limit is not None:
        filters = filters.model_copy(update={"limit": limit})
    cleared_only = cleared_only or filters.cleared_only
    if cleared_only and not filters.cleared_only:
        # Let providers narrow their query to cleared licenses; the post-filter below stays.
        filters = filters.model_copy(update={"cleared_only": True})
    if provider is not None:
        try:
            providers = [registry.get(provider)]
        except KeyError as exc:
            raise LookupError(exc.args[0]) from None
    else:
        providers = registry.providers_for(filters.kind)
    cache: Cache | None
    try:
        cache = Cache()
    except (sqlite3.Error, OSError) as exc:
        (warn or logger.warning)(f"cache unavailable: {exc}")
        cache = None
    try:
        per_provider, errors = registry.search_each(
            query, filters, providers=providers, cache=cache, use_cache=use_cache, dropped=dropped
        )
    finally:
        if cache is not None:
            cache.close()
    # search_each returns one list per target provider, in the order given.
    weights = [provider_weight(p.id, filters.style, filters.kind) for p in providers]
    assets = interleave_weighted(per_provider, weights)
    if filters.shorts and filters.kind == Kind.VIDEO:
        assets = _order_for_shorts(assets, filters.style)
    if cleared_only:
        assets = [a for a in assets if rights.is_cleared(rights.classify(a).level)]
    if limit is not None:
        assets = assets[:limit]
    return assets, errors


DEFAULT_EXPANDED_LIMIT = 20
"""``search_expanded`` budget when neither ``limit`` nor ``filters.limit`` is set."""
RELATED_LIMIT = 6
MAX_EXPANSIONS = 3
RELATED_SFX_SUFFIX = " sound effect"
EXPAND_STYLES: frozenset[str] = frozenset({"meme", "green_screen"})


def _asset_key(asset: Asset) -> tuple[str, str]:
    return (asset.provider, asset.id)


def _add_unique(
    out: list[Asset], seen: set[tuple[str, str]], assets: Sequence[Asset], cap: int | None = None
) -> None:
    """Append assets not yet in ``seen`` (by provider + id) to ``out``, up to ``cap`` total."""
    for asset in assets:
        if cap is not None and len(out) >= cap:
            return
        key = _asset_key(asset)
        if key not in seen:
            seen.add(key)
            out.append(asset)


def _add_errors(
    out: list[ProviderError], seen: set[tuple[str, str]], errors: Sequence[ProviderError]
) -> None:
    for err in errors:
        key = (err.provider, err.message)
        if key not in seen:
            seen.add(key)
            out.append(err)


CLEARED_RELAX_FILLER: frozenset[str] = frozenset(
    {
        "background", "music", "bgm", "song", "songs", "track", "tracks", "audio", "sound",
        "sounds", "instrumental", "beat", "beats", "sfx", "free", "royalty",
    }
)  # fmt: skip
"""Generic words ``relax_query`` drops (plus the phrase "sound effect(s)")."""
_SOUND_EFFECT_RE = re.compile(r"\bsound[\s-]+effects?\b", re.IGNORECASE)


def relax_query(query: str) -> str:
    """``query`` without generic filler ("phonk background music" -> "phonk"); may be ``""``.

    Case-insensitive; the phrase "sound effect(s)" and ``CLEARED_RELAX_FILLER`` words are
    removed and whitespace is collapsed.
    """
    text = _SOUND_EFFECT_RE.sub(" ", query)
    words = [
        w for w in text.split() if w.strip("\"'.,!?;:()[]{}").lower() not in CLEARED_RELAX_FILLER
    ]
    return " ".join(words)


KYM_MIN_RESULTS = 3
"""``search_expanded`` asks Know Your Meme for names only when the primary search found fewer."""
KYM_MAX_NAMES = 2


def online_recall(
    query: str,
    kind: Kind | None = None,
    *,
    safe: bool | None = None,
    block_sitemap: bool = False,
    on_sitemap_fetch: Callable[[], None] | None = None,
) -> list[recall_online.RecallHit]:
    """``recall_online.kym_lookup`` with the user ``cache.db`` (uncached if it is unavailable).

    ``safe`` None means ``safesearch.configured_safe()`` (sensitive entries are fuzzy candidates
    only with safe search off). ``block_sitemap`` / ``on_sitemap_fetch``: see ``kym_lookup``;
    the default never waits on the sitemap. Raises ``ProviderError`` (provider ``knowyourmeme``) when scraping is disabled or the
    lookup fails.
    """
    cache: Cache | None
    try:
        cache = Cache()
    except (sqlite3.Error, OSError):
        cache = None
    try:
        return recall_online.kym_lookup(
            query,
            kind,
            cache=cache,
            safe=safesearch.resolve_safe(safe),
            block_sitemap=block_sitemap,
            on_sitemap_fetch=on_sitemap_fetch,
        )
    finally:
        if cache is not None:
            cache.close()


def online_recall_names(
    query: str,
    kind: Kind | None = None,
    *,
    limit: int = 3,
    warn: Warn | None = None,
    safe: bool | None = None,
) -> list[str]:
    """Up to ``limit`` Know Your Meme names for ``query``; [] when disabled or on any failure.

    Never waits on the sitemap (``online_recall`` default); ``safe`` as in ``online_recall``.
    """
    try:
        if not scrape.enabled():
            return []
        hits = online_recall(query, kind, safe=safe)
    except Exception as exc:  # noqa: BLE001 - the online lookup is best effort
        (warn or logger.warning)(f"knowyourmeme lookup failed: {exc}")
        return []
    return recall_online.hit_names(hits, query, limit=limit)


def _kym_names(
    query: str,
    kind: Kind,
    errors: list[ProviderError],
    error_keys: set[tuple[str, str]],
    *,
    safe: bool,
) -> list[str]:
    """Up to ``KYM_MAX_NAMES`` Know Your Meme names for ``query``; [] when scraping is off.

    Never waits on the sitemap (a missing one is fetched in the background). A lookup failure
    is added to ``errors`` (provider ``knowyourmeme``) and gives [].
    """
    try:
        if not scrape.enabled():
            return []
        hits = online_recall(query, kind, safe=safe)
    except ProviderError as exc:
        _add_errors(errors, error_keys, [exc])
        return []
    except Exception as exc:  # noqa: BLE001 - the online lookup is best effort
        message = f"{type(exc).__name__}: {exc}"
        _add_errors(errors, error_keys, [ProviderError("knowyourmeme", message)])
        return []
    return recall_online.hit_names(hits, query, limit=KYM_MAX_NAMES)


def search_expanded(
    query: str,
    filters: SearchFilters,
    *,
    provider: str | None = None,
    use_cache: bool = True,
    limit: int | None = None,
    warn: Warn | None = None,
    cleared_only: bool = False,
) -> SearchOutcome:
    """``search`` plus offline recall expansions and, for audio, the related audio kind.

    Expansions (``recall.expand_query``) run when ``filters.expand`` is set and the primary
    search came back short, the style is meme / green_screen, or the kind is audio; each runs
    with ``max(limit // 2, 5)`` and only adds unseen ``(provider, id)`` results, up to
    ``2 * limit`` in total. Expansions of the raw query and of ``recall.core_query`` (filler
    such as "bgm" stripped) are unioned, deduped, at most ``MAX_EXPANSIONS``. When the primary
    search found fewer than ``KYM_MIN_RESULTS`` and scraping is enabled, up to ``KYM_MAX_NAMES``
    Know Your Meme names (``recall_online.kym_lookup``) are appended as further expansions.

    With ``filters.related`` and an audio kind, the core query is also searched in the other
    audio kind into ``related`` (``RELATED_LIMIT`` in total): if it finds nothing and differs
    from the raw query, the raw query is tried once; for related ``audio_sfx`` with a one-word
    core query, ``"<core> sound effect"`` is tried too. Related results are noise-filtered
    (``relevance``): ``max_duration`` is capped per related kind (unknown lengths kept) and
    titles matching ``relevance.RELATED_NOISE_PATTERNS`` (DJ mixes, audiobooks, "10 hours") are
    dropped. For an audio kind and a query of two or more words, off-topic primary results (a
    noise title, or no content token shared with the query and a length that is unknown or over
    the kind's cap) are moved after the rest (stable, see ``relevance.demote_off_topic``);
    expansions and related are not reordered. Errors from
    every call are merged (identical provider + message once). ``cleared_only`` applies to every
    call (``assets`` and ``related``), see ``search``. ``filtered_count`` counts the distinct
    (provider + id) results safe search hid across all calls. Raises ``LookupError`` like
    ``search``.

    With ``cleared_only`` and an audio kind, a primary search with no results is retried once
    with ``relax_query(query)`` (if non-empty and different); when that finds results they
    become the primary results and ``relaxed_query`` is set (the off-topic demotion then uses
    the relaxed query).
    """
    budget = limit or filters.limit or DEFAULT_EXPANDED_LIMIT
    # Providers ignore expand/related; normalise them so the toggles never split cache keys.
    inner = filters.model_copy(update={"expand": True, "related": True})
    dropped: list[Asset] = []

    def run(q: str, f: SearchFilters, n: int) -> tuple[list[Asset], list[ProviderError]]:
        return search(
            q,
            f,
            provider=provider,
            use_cache=use_cache,
            limit=n,
            warn=warn,
            cleared_only=cleared_only,
            dropped=dropped,
        )

    primary, primary_errors = run(query, inner, budget)
    relaxed_query: str | None = None
    if cleared_only and not primary and filters.kind in recall.AUDIO_KINDS:
        relaxed = relax_query(query)
        if relaxed and relaxed != " ".join(query.split()):
            retry, retry_errors = run(relaxed, inner, budget)
            primary_errors = [*primary_errors, *retry_errors]
            if retry:
                primary, relaxed_query = retry, relaxed
    if filters.kind in recall.AUDIO_KINDS:
        primary = relevance.demote_off_topic(relaxed_query or query, primary, filters.kind)
    assets: list[Asset] = []
    seen: set[tuple[str, str]] = set()
    _add_unique(assets, seen, primary)
    errors: list[ProviderError] = []
    error_keys: set[tuple[str, str]] = set()
    _add_errors(errors, error_keys, primary_errors)

    expanded: list[str] = []
    audio = filters.kind in recall.AUDIO_KINDS
    core = recall.core_query(query, filters.kind)
    if filters.expand and (len(assets) < budget or filters.style in EXPAND_STYLES or audio):
        cap = 2 * budget
        names = recall.expand_query(query, filters.kind)
        if core != query:
            names += recall.expand_query(core, filters.kind)
        names = list(dict.fromkeys(names))[:MAX_EXPANSIONS]
        if len(assets) < KYM_MIN_RESULTS:
            online = _kym_names(query, filters.kind, errors, error_keys, safe=filters.safe)
            names += [n for n in online if n not in names]
        for name in names:
            if len(assets) >= cap:
                break
            extra, extra_errors = run(name, inner, max(budget // 2, 5))
            expanded.append(name)
            _add_unique(assets, seen, extra, cap)
            _add_errors(errors, error_keys, extra_errors)

    related: list[Asset] = []
    related_kind = recall.related_audio_kind(filters.kind) if filters.related else None
    # A single-provider search only gets related results that provider can serve.
    if (
        related_kind is not None
        and provider is not None
        and related_kind not in registry.get(provider).kinds
    ):
        related_kind = None
    if related_kind is not None:
        related_filters = inner.model_copy(
            update={
                "kind": related_kind,
                "max_duration": relevance.related_max_duration(related_kind, inner.max_duration),
            }
        )

        def run_related(q: str) -> int:
            """Search ``q`` in the related kind; return how many non-noise results it found."""
            other, other_errors = run(q, related_filters, RELATED_LIMIT)
            other = relevance.drop_related_noise(other)
            _add_unique(related, seen, other, RELATED_LIMIT)
            _add_errors(errors, error_keys, other_errors)
            return len(other)

        if run_related(core) == 0 and core != query:
            run_related(query)
        if (
            related_kind == Kind.AUDIO_SFX
            and len(core.split()) == 1
            and len(related) < RELATED_LIMIT
        ):
            run_related(core + RELATED_SFX_SUFFIX)

    return SearchOutcome(
        assets=assets,
        errors=errors,
        expanded_queries=expanded,
        related=related,
        related_kind=related_kind,
        filtered_count=len({_asset_key(a) for a in dropped}),
        relaxed_query=relaxed_query,
    )


def resolve_ref(ref: str) -> tuple[Provider, Asset]:
    """Return the provider and cached asset for ``<provider>:<id>``.

    Raises ``RefError`` (bad ref, unknown provider, not cached) or ``CacheUnavailableError``.
    """
    registry.load_builtins()
    provider_id, asset_id = parse_ref(ref)
    if provider_id not in _known_providers():
        raise _bad_ref(ref)
    try:
        with Cache() as cache:
            asset = cache.find_asset(provider_id, asset_id)
    except (sqlite3.Error, OSError) as exc:
        raise CacheUnavailableError(f"cache unavailable: {exc}") from None
    if asset is None:
        raise RefError(
            f"{ref} is not in the search cache; run a search first so the asset is cached",
            hint="run a search first so the asset is cached",
        )
    return registry.get(provider_id), asset


# Asset kinds a --clip range applies to (http plans are cut with ffmpeg).
CLIP_KINDS = frozenset({Kind.VIDEO, Kind.AUDIO_SFX, Kind.AUDIO_MUSIC})


def download_ref(
    ref: str,
    *,
    out: Path | None = None,
    mp3: bool = False,
    clip: tuple[float, float] | None = None,
    cookies_from_browser: str | None = None,
    progress: ProgressHook | None = None,
    max_wall_s: float | None = None,
    quality: str | None = None,
) -> tuple[str, DownloadResult]:
    """Download a cached ``<provider>:<id>`` asset; return ``(url downloaded, result)``.

    ``mp3`` is a no-op for ``http`` plans whose file is already mp3; other ``http`` plans
    reject it with ``RefError``. ``clip`` is cut with ffmpeg for http
    assets whose kind is in ``CLIP_KINDS`` and rejected (``RefError``) for anything else.
    ``max_wall_s`` limits an http clip re-encode (0 or less: no limit); None falls back to
    ``[download] clip_max_wall_s`` / ``CLIPFORAGE_CLIP_MAX_WALL_S``, then the 300 s default.
    ``quality`` (see ``resolve_quality``) caps yt-dlp video height; http plans ignore it.
    Download failures propagate
    (``DownloadError``, ``ProviderError``, ``httpx.HTTPError``, ``OSError``, ``ValueError``).
    """
    provider, asset = resolve_ref(ref)
    plan = provider.resolve(asset)
    if quality is not None or plan.method == "ytdlp":  # http plans never read the setting
        quality = resolve_quality(quality)
    if plan.method == "http" and mp3:
        ext = plan.ext.lstrip(".").lower()
        if ext != "mp3":  # a direct mp3 file already is what --mp3 asks for
            raise RefError(
                "--mp3 only applies to yt-dlp sources",
                hint=f"download without mp3; this source is a direct {ext or 'unknown'} file",
            )
    if plan.method == "http" and clip and asset.kind not in CLIP_KINDS:
        raise RefError(
            "clip only applies to video/audio sources",
            hint=f"download without clip; this source is a {asset.kind} asset",
        )
    if plan.method == "ytdlp" and (mp3 or clip):
        result = download_url(
            plan.url,
            out,
            audio_only=mp3 or plan.ext.lstrip(".") == "mp3",  # audio-kind plans stay mp3
            clip=clip,
            progress=progress,
            cookies_from_browser=cookies_from_browser,
            quality=quality,
        )
    else:
        if max_wall_s is None and clip:
            max_wall_s = _clip_max_wall_setting()
        result = download_plan(
            plan,
            out,
            progress=progress,
            cookies_from_browser=cookies_from_browser,
            clip=clip,
            max_wall_s=max_wall_s,
            quality=quality,
        )
    return plan.url, result


def _clip_max_wall_setting() -> float | None:
    """``[download] clip_max_wall_s`` (or ``CLIPFORAGE_CLIP_MAX_WALL_S``) as a float, else None."""
    raw = get_setting("download", "clip_max_wall_s", env="CLIP_MAX_WALL_S")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        logger.warning(f"ignoring clip_max_wall_s {raw!r}: not a number")
        return None
    return value


def resolve_quality(quality: str | None) -> str | None:
    """Explicit ``quality``, else ``[download] quality`` / ``CLIPFORAGE_QUALITY``, else None.

    Raises ``RefError`` if the chosen value is not one of ``QUALITIES``.
    """
    if quality is None:
        quality = get_setting("download", "quality", env="QUALITY")
    if quality is not None and quality not in QUALITIES:
        allowed = ", ".join(QUALITIES)
        raise RefError(
            f"unknown quality {quality!r}; use one of {allowed}",
            hint=f"pass a quality from: {allowed} (or fix [download] quality in config.toml)",
        )
    return quality


def download_link(
    url: str,
    *,
    out: Path | None = None,
    mp3: bool = False,
    clip: tuple[float, float] | None = None,
    cookies_from_browser: str | None = None,
    progress: ProgressHook | None = None,
    quality: str | None = None,
) -> DownloadResult:
    """Download any yt-dlp-supported ``url``. Raises ``DownloadError`` on failure.

    ``quality`` falls back to the config setting (see ``resolve_quality``, ``RefError`` if bad).
    """
    return download_url(
        url,
        out,
        audio_only=mp3,
        clip=clip,
        progress=progress,
        cookies_from_browser=cookies_from_browser,
        quality=resolve_quality(quality),
    )


def _asset_from_meta(meta: dict[str, Any]) -> Asset:
    """Rebuild the ``Asset`` a sidecar was written for (raises ``KeyError`` / ``ValueError``)."""
    return Asset(
        provider=meta["provider"],
        id=meta["asset_id"],
        kind=meta["kind"],
        title=meta["title"],
        source_url=meta["source_url"],
        license=meta["license"],
        attribution_text=meta.get("attribution_text", ""),
        commercial_ok=meta.get("commercial_ok"),
    )


def record_history(url: str, result: DownloadResult, *, warn: Warn | None = None) -> dict[str, Any]:
    """Record a finished download in cache.db and return its sidecar metadata.

    The returned dict also carries ``history_id``: the new history row id, or None if the
    row could not be written. A history failure never raises: ``warn`` (default: the
    ``clipforage`` logger) gets ``could not record download history: ...`` and the metadata
    is still returned.
    """
    meta: dict[str, Any] = json.loads(result.sidecar.read_text(encoding="utf-8"))
    history_id: int | None = None
    try:
        asset = _asset_from_meta(meta)
        extra = {k: v for k, v in meta.items() if k not in _SIDECAR_BASE_KEYS}
        with Cache() as cache:
            history_id = cache.record_download(
                url=url,
                path=result.path,
                sidecar=result.sidecar,
                asset=asset,
                fetched_at=result.fetched_at,
                extra=extra,
            )
    except Exception as exc:  # noqa: BLE001 - history must never fail a download
        (warn or logger.warning)(f"could not record download history: {exc}")
    return {**meta, "history_id": history_id}


def history(limit: int = 20) -> list[dict[str, Any]]:
    """Return up to ``limit`` downloads, newest first. Raises ``CacheUnavailableError``."""
    try:
        with Cache() as cache:
            return cache.history(limit)
    except (sqlite3.Error, OSError) as exc:
        raise CacheUnavailableError(f"cache unavailable: {exc}") from None


KEYOUT_EXTS: frozenset[str] = frozenset({".mp4", ".webm", ".mkv", ".mov"})
"""Video file suffixes ``key_out_download`` / ``key_out_path`` accept."""

KEYED_SUFFIX = ".key.webm"
"""Suffix of ``key_out`` exports; these are refused as keyout sources."""


def _check_keyout_source(src: Path) -> None:
    """Raise ``RefError`` unless ``src`` is an existing video file (``KEYOUT_EXTS``) that is
    not already a keyed export (``KEYED_SUFFIX``)."""
    if src.suffix.lower() not in KEYOUT_EXTS:
        allowed = ", ".join(sorted(e.lstrip(".") for e in KEYOUT_EXTS))
        raise RefError(
            f"cannot key out {src.name}: not a video file",
            hint=f"key out a video download ({allowed})",
        )
    if src.name.lower().endswith(KEYED_SUFFIX):
        raise RefError(
            f"cannot key out {src.name}: already a keyed export ({KEYED_SUFFIX})",
            hint="key out the original video download instead",
        )
    if not src.is_file():
        raise RefError(
            f"file no longer exists: {src}",
            hint="download it again, then run `clipforage history` for the new id",
        )


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    """Return a sidecar's JSON, or None if it is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _keyout_sidecar(out: Path, src: Path, meta: dict[str, Any], tolerance: int) -> Path:
    """Write ``out``'s sidecar: the source's license meta plus ``format``/``derived_from``/
    ``chroma``. Raises ``KeyError`` / ``ValueError`` if ``meta`` lacks the base keys."""
    asset = _asset_from_meta(meta)
    extra = {
        k: v
        for k, v in meta.items()
        if k not in _SIDECAR_BASE_KEYS and k not in ("clipforage_version", "history_id")
    }
    extra.update(
        {
            "format": "webm",
            "derived_from": src.name,
            "chroma": {
                "tolerance": tolerance,
                "similarity": chroma_similarity(tolerance),
                "blend": KEY_BLEND,
            },
        }
    )
    return write_sidecar(out, asset, datetime.now(UTC), extra)


def _run_key_out(
    src: Path, tolerance: int, progress: ProgressHook | None, max_wall_s: float | None
) -> Path:
    if max_wall_s is None:
        max_wall_s = _clip_max_wall_setting()
    if max_wall_s is None:
        return dl.key_out(src, tolerance=tolerance, progress=progress)
    return dl.key_out(src, tolerance=tolerance, progress=progress, max_wall_s=max_wall_s)


def key_out_download(
    history_id: int,
    *,
    tolerance: int = 80,
    progress: ProgressHook | None = None,
    max_wall_s: float | None = None,
    warn: Warn | None = None,
) -> dict[str, Any]:
    """Key out the green screen of history row ``history_id`` into a ``.key.webm`` beside it.

    Writes a sidecar (source license meta + ``format``/``derived_from``/``chroma``), records a
    new history row with the source row's ``url`` and returns the sidecar meta plus ``path``,
    ``sidecar`` and ``history_id``. ``max_wall_s`` None falls back to ``[download]
    clip_max_wall_s``, then 300 s. Raises ``RefError`` (unknown id, not a video, file gone),
    ``CacheUnavailableError`` or ``DownloadError``.
    """
    try:
        with Cache() as cache:
            row = cache.get_download(history_id)
    except (sqlite3.Error, OSError) as exc:
        raise CacheUnavailableError(f"cache unavailable: {exc}") from None
    if row is None:
        raise RefError(
            f"no download with id {history_id}",
            hint="run `clipforage history` to list download ids",
        )
    src = Path(row["path"])
    _check_keyout_source(src)
    out = _run_key_out(src, tolerance, progress, max_wall_s)
    # The source sidecar wins; the history row fills keys it lacks (or the whole thing if gone).
    meta = {
        "provider": row["provider"],
        "asset_id": row["asset_id"],
        "kind": Kind.VIDEO,
        "title": row["title"],
        "source_url": row["url"],
        "license": row["license"],
        **row["extra"],
        **(_read_sidecar(Path(row["sidecar"])) or {}),
    }
    sidecar = _keyout_sidecar(out, src, meta, tolerance)
    result = DownloadResult(path=out, sidecar=sidecar, fetched_at=datetime.now(UTC))
    recorded = record_history(row["url"], result, warn=warn)
    return {**recorded, "path": str(out), "sidecar": str(sidecar)}


def key_out_path(
    src: Path,
    *,
    tolerance: int = 80,
    progress: ProgressHook | None = None,
    max_wall_s: float | None = None,
) -> dict[str, Any]:
    """Key out a video file directly; returns ``{"path", "sidecar"}``.

    A sidecar is written only if ``src`` has a readable one (``sidecar`` is None otherwise);
    no history row is recorded. Raises ``RefError`` or ``DownloadError``.
    """
    _check_keyout_source(src)
    out = _run_key_out(src, tolerance, progress, max_wall_s)
    meta = _read_sidecar(sidecar_path(src))
    sidecar: Path | None = None
    if meta is not None:
        try:
            sidecar = _keyout_sidecar(out, src, meta, tolerance)
        except (KeyError, ValueError) as exc:
            logger.warning(f"could not copy sidecar from {sidecar_path(src).name}: {exc}")
    return {"path": str(out), "sidecar": str(sidecar) if sidecar else None}


PHRASE_SOURCES: tuple[str, ...] = ("youtube", "internet_archive")
"""Sources phrase search can read captions from (YouTube captions, IA subtitle files)."""
_IA_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _youtube_for_phrase() -> Any:
    from clipforage.download.service import parse_cookies_from_browser
    from clipforage.providers.youtube import YouTubeProvider

    spec = get_setting("download", "cookies_from_browser", env="COOKIES_FROM_BROWSER")
    cookies: tuple[str | None, ...] | None = None
    if spec:
        try:
            cookies = parse_cookies_from_browser(spec)
        except ValueError:
            cookies = None  # a bad setting must not break search; `get` reports it
    return YouTubeProvider(cookies_from_browser=cookies)


def _phrase_candidates(
    source: str, query: str, within: str | None, max_videos: int, warn: Warn
) -> list[Asset]:
    """Assets whose captions may contain ``query``. Raises ``ProviderError``."""
    if source == "youtube":
        provider = _youtube_for_phrase()
        if within and within.startswith(("http://", "https://")):
            return provider.assets_from_url(within, max_videos)
        if within:
            warn(f"--within {within!r} is not a URL; searching YouTube instead")
        filters = SearchFilters(kind=Kind.VIDEO, style="any", limit=max_videos)
        return provider.search(query, Kind.VIDEO, filters)
    from clipforage.providers.internet_archive import InternetArchiveProvider

    # IA search matches item metadata, not subtitle text: films are found by title (or
    # ``within`` as identifier / title); quotes then match inside already-indexed films.
    ia = InternetArchiveProvider()
    if within:
        if _IA_IDENTIFIER_RE.match(within):
            found = ia.search_with_subtitles(f"identifier:{within}", max_videos)
            if found:
                return found
        return ia.search_with_subtitles(within, max_videos)
    found = ia.search_with_subtitles(query, max_videos)
    # Nothing matched the query: index the most-downloaded subtitled feature films instead.
    return found or ia.search_with_subtitles("", max_videos)


def _fetch_captions(asset: Asset) -> Any:
    from clipforage.phrase import captions

    if asset.provider == "youtube":
        return captions.fetch_youtube_captions(asset)
    return captions.fetch_ia_captions(asset)


def phrase_search(
    query: str,
    *,
    sources: Sequence[str] | None = None,
    within: str | None = None,
    max_videos: int = 10,
    limit: int = 20,
    use_cache: bool = True,
    warn: Warn | None = None,
    max_per_video: int = 3,
    safe: bool | None = None,
) -> PhraseOutcome:
    """Find ``query`` spoken in YouTube captions / Internet Archive subtitles.

    For each source, up to ``max_videos`` candidate videos are found (YouTube: search, or the
    flat entries of the ``within`` playlist/channel/video URL; Internet Archive: movies with
    SubRip/WebVTT files whose metadata matches ``within`` (an identifier, else a title) or the
    query, falling back to the most-downloaded subtitled feature films). Captions not yet
    indexed (or all, when ``use_cache`` is False) are fetched into cache.db; then the whole
    caption index (including videos indexed by earlier runs) is searched for the phrase, so
    each run grows the local index. At most ``max_per_video`` hits per video (0 = no cap)
    are kept before ``limit``. Hit assets are stored in the search cache so
    ``clipforage get <provider>:<id>`` can resolve them.

    Raises ``LookupError`` for an unknown source and ``CacheUnavailableError`` if cache.db
    (or SQLite FTS5) is unavailable. A failing source becomes a ``ProviderError`` entry; a
    failing caption fetch is reported through ``warn`` and skipped.

    Safe search (``safe``; None means ``safesearch.configured_safe()``) skips explicit
    candidates (``safesearch.is_explicit``, title-based for videos) before their captions are
    fetched and drops explicit hits found in the existing caption index;
    ``PhraseOutcome.filtered_count`` counts the distinct (provider + id) assets dropped by both.
    """
    warn_fn = warn or logger.warning
    safe_on = safesearch.resolve_safe(safe)
    targets = list(dict.fromkeys(sources)) if sources else list(PHRASE_SOURCES)
    for source in targets:
        if source not in PHRASE_SOURCES:
            raise LookupError(
                f"unknown phrase source {source!r}; choose from: {', '.join(PHRASE_SOURCES)}"
            )
    errors: list[ProviderError] = []
    dropped: set[tuple[str, str]] = set()
    try:
        with Cache() as cache:
            for source in targets:
                try:
                    candidates = _phrase_candidates(source, query, within, max_videos, warn_fn)
                except ProviderError as exc:
                    errors.append(exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - one source must not fail the search
                    errors.append(ProviderError(source, f"{type(exc).__name__}: {exc}"))
                    continue
                if safe_on:
                    dropped.update(_asset_key(a) for a in candidates if safesearch.is_explicit(a))
                    candidates, _ = safesearch.filter_explicit(candidates)
                for asset in candidates:
                    if use_cache and cache.has_captions(asset.provider, asset.id):
                        continue
                    try:
                        found = _fetch_captions(asset)
                    except Exception as exc:  # noqa: BLE001 - skip this video, keep going
                        warn_fn(f"{asset.provider}:{asset.id}: could not fetch captions: {exc}")
                        continue
                    cues, lang, auto = found if found is not None else ([], None, False)
                    cache.put_captions(asset, cues, lang=lang, auto_generated=auto)
            rows = cache.search_captions(
                query, providers=targets, limit=limit, max_per_asset=max(max_per_video, 0)
            )
            if safe_on:
                dropped.update(
                    _asset_key(row.asset) for row in rows if safesearch.is_explicit(row.asset)
                )
            hits = [
                PhraseHit(
                    asset=row.asset,
                    start_s=row.start_s,
                    end_s=row.end_s,
                    text=row.text,
                    lang=row.lang,
                    auto_generated=row.auto_generated,
                )
                for row in rows
                if not (safe_on and safesearch.is_explicit(row.asset))
            ]
            _cache_hit_assets(cache, query, hits)
    except (sqlite3.Error, OSError) as exc:
        raise CacheUnavailableError(f"cache unavailable: {exc}") from None
    return PhraseOutcome(hits=hits, errors=errors, filtered_count=len(dropped))


def _cache_hit_assets(cache: Cache, query: str, hits: Sequence[PhraseHit]) -> None:
    """Store hit assets in the search cache so ``<provider>:<id>`` refs resolve for ``get``."""
    registry.load_builtins()
    by_provider: dict[str, dict[str, Asset]] = {}
    for hit in hits:
        by_provider.setdefault(hit.asset.provider, {})[hit.asset.id] = hit.asset
    for provider_id, assets in by_provider.items():
        try:
            ttl = int(getattr(registry.get(provider_id), "SEARCH_TTL_S", 3600))
        except KeyError:
            ttl = 3600
        cache.put_search(
            provider_id,
            f"phrase:{query}",
            SearchFilters(kind=Kind.VIDEO),
            list(assets.values()),
            ttl_s=ttl,
        )
