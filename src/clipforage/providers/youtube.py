"""YouTube provider: video and audio search via yt-dlp's flat search extractors.

No key required. Search runs on the user's machine only; this provider is never hosted.
Results are third-party content with an unknown license (`license="unknown"`,
`commercial_ok=None`); downloads are resolved to a yt-dlp `DownloadPlan`.

`filters.style` appends an intent term to the query (unless already present) and sets a
default duration cap; short caps use YouTube's "video, under 4 minutes" results filter.
`filters.shorts` (video only) appends " shorts", caps duration at 60 s, fetches a larger pool
and puts portrait first (best effort: only `/shorts/` entry URLs are known to be portrait).
Audio kinds (`audio_music`, `audio_sfx`) search the same way but never get the style term, use
their own duration caps, and resolve to an mp3 download (yt-dlp FFmpegExtractAudio).
"""

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote_plus

import yt_dlp

from clipforage.models import (
    SHORTS_MAX_DURATION_S,
    Asset,
    DownloadPlan,
    Kind,
    Orientation,
    SearchFilters,
    Style,
)
from clipforage.providers.base import ProviderError

_SKIP_LIVE_STATUS = ("is_live", "is_upcoming")
_EXTRA_KEYS = ("channel", "channel_url", "view_count", "live_status", "uploader_id")

# style -> (term appended to the query, regex that means the query already has the intent)
_STYLE_TERMS: dict[Style, tuple[str, re.Pattern[str]]] = {
    "meme": (" meme", re.compile(r"meme|green ?screen|template", re.IGNORECASE)),
    "green_screen": (" green screen", re.compile(r"green ?screen|chroma", re.IGNORECASE)),
    "cinematic": (
        " cinematic b-roll",
        re.compile(r"b-?roll|cinematic|footage|stock", re.IGNORECASE),
    ),
}

_SHORTS_TERM = (" shorts", re.compile(r"\bshorts\b", re.IGNORECASE))

MEME_MAX_DURATION_S = 120.0
GREEN_SCREEN_MAX_DURATION_S = 120.0
CINEMATIC_MAX_DURATION_S = 600.0
ANY_MAX_DURATION_S = 600.0
_STYLE_MAX_DURATION_S: dict[Style, float] = {
    "meme": MEME_MAX_DURATION_S,
    "green_screen": GREEN_SCREEN_MAX_DURATION_S,
    "cinematic": CINEMATIC_MAX_DURATION_S,
    "any": ANY_MAX_DURATION_S,
}


AUDIO_SFX_MAX_DURATION_S = 60.0
AUDIO_MUSIC_MAX_DURATION_S = 900.0
_AUDIO_MAX_DURATION_S: dict[Kind, float] = {
    Kind.AUDIO_SFX: AUDIO_SFX_MAX_DURATION_S,
    Kind.AUDIO_MUSIC: AUDIO_MUSIC_MAX_DURATION_S,
}
AUDIO_KINDS: frozenset[Kind] = frozenset(_AUDIO_MAX_DURATION_S)

SHORT_FILTER_MAX_S = 240.0
"""At or below this cap, search with YouTube's "under 4 minutes" filter."""
# `sp=` is YouTube's undocumented protobuf filter code: type=video + duration under 4 min.
SHORT_SEARCH_URL = "https://www.youtube.com/results?search_query={q}&sp=EgQQARgB"
MIN_FETCH = 20
MAX_FETCH = 60
FETCH_FACTOR = 3
SHORTS_MAX_FETCH = 90
"""``filters.shorts`` fetches ``min(FETCH_FACTOR * pool, SHORTS_MAX_FETCH)`` (most are > 60 s)."""


def augment_query(query: str, style: Style, shorts: bool = False) -> str:
    """Append the style's intent term (and " shorts") unless the query already expresses it."""
    term = _STYLE_TERMS.get(style)
    if term is not None and not term[1].search(query):
        query += term[0]
    if shorts and not _SHORTS_TERM[1].search(query):
        query += _SHORTS_TERM[0]
    return query


def effective_max_duration(filters: SearchFilters) -> float:
    """``filters.max_duration`` if set, else the audio kind's or the style's default cap (s).

    ``filters.shorts`` on a video search lowers the result to at most 60 s.
    """
    if filters.max_duration is not None:
        cap = filters.max_duration
    elif filters.kind in _AUDIO_MAX_DURATION_S:
        return _AUDIO_MAX_DURATION_S[filters.kind]
    else:
        cap = _STYLE_MAX_DURATION_S[filters.style]
    if filters.shorts and filters.kind == Kind.VIDEO:
        return min(cap, SHORTS_MAX_DURATION_S)
    return cap


class YouTubeProvider:
    """Searches YouTube with yt-dlp in flat mode (metadata only, no download)."""

    id = "youtube"
    kinds = frozenset({Kind.VIDEO, Kind.AUDIO_MUSIC, Kind.AUDIO_SFX})
    requires_key = False
    SEARCH_TTL_S = 3600
    MAX_RESULTS = 10

    def __init__(
        self,
        max_results: int = MAX_RESULTS,
        ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
        cookies_from_browser: tuple[str | None, ...] | None = None,
    ) -> None:
        self.max_results = max_results
        self.cookies_from_browser = cookies_from_browser
        self._ydl_factory = ydl_factory if ydl_factory is not None else yt_dlp.YoutubeDL

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        """Return up to `filters.limit` (else `max_results`) short-enough assets of `kind`.

        Audio kinds skip the style term and use their own duration cap. Raises ProviderError on
        any failure.
        """
        if kind not in self.kinds:
            return []
        audio = kind in AUDIO_KINDS
        search_query = query if audio else augment_query(query, filters.style, filters.shorts)
        max_duration = effective_max_duration(filters.model_copy(update={"kind": kind}))
        want = filters.limit or self.max_results
        fetch = min(max(FETCH_FACTOR * (filters.limit or 10), MIN_FETCH), MAX_FETCH)
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
        }
        if max_duration <= SHORT_FILTER_MAX_S:
            url = SHORT_SEARCH_URL.format(q=quote_plus(search_query))
            if filters.shorts and not audio:
                fetch = min(FETCH_FACTOR * fetch, SHORTS_MAX_FETCH)
            opts["playlistend"] = fetch
        else:
            url = f"ytsearch{fetch}:{search_query}"
        if self.cookies_from_browser:
            opts["cookiesfrombrowser"] = self.cookies_from_browser
        try:
            with self._ydl_factory(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = (info or {}).get("entries") or []
            assets: list[Asset] = []
            for entry in entries:
                if (
                    entry is None
                    or not entry.get("id")
                    or entry.get("live_status") in _SKIP_LIVE_STATUS
                ):
                    continue
                age_limit = entry.get("age_limit")
                if filters.safe and isinstance(age_limit, int) and age_limit >= 18:
                    # Inert in practice: flat search results carry no age_limit (verified live
                    # 2026-10-03, yt-dlp 2026.08.19), so YouTube safe search relies on the
                    # shared blocklist post-filter in safesearch.py.
                    continue
                duration = entry.get("duration")
                if duration is None or duration > max_duration:
                    continue
                if filters.min_duration is not None and duration < filters.min_duration:
                    continue
                assets.append(self._to_asset(entry, search_query, kind))
                if len(assets) >= want:
                    break
            if filters.shorts and not audio:
                assets.sort(key=lambda a: a.orientation != "portrait")  # stable: portrait first
            return assets
        except yt_dlp.utils.DownloadError as e:
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from e
        except Exception as e:  # never let any other exception escape search
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from e

    def assets_from_url(self, url: str, limit: int) -> list[Asset]:
        """Return up to ``limit`` video assets from a playlist / channel / video URL.

        Uses flat extraction (metadata only, no duration filter; live and upcoming entries
        are skipped). A single-video URL yields that one video. Raises ProviderError.
        """
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
            "playlistend": max(1, limit),
        }
        if self.cookies_from_browser:
            opts["cookiesfrombrowser"] = self.cookies_from_browser
        try:
            with self._ydl_factory(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            info = info or {}
            entries = info.get("entries")
            if entries is None:
                entries = [info] if info.get("id") else []
            assets: list[Asset] = []
            for entry in entries:
                if (
                    entry is None
                    or not entry.get("id")
                    or entry.get("live_status") in _SKIP_LIVE_STATUS
                ):
                    continue
                assets.append(self._to_asset(entry, url))
                if len(assets) >= limit:
                    break
            return assets
        except Exception as e:  # never let any other exception escape
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from e

    def _to_asset(self, entry: dict[str, Any], search_query: str, kind: Kind = Kind.VIDEO) -> Asset:
        video_id = entry["id"]
        title = entry.get("title") or video_id
        source_url = f"https://www.youtube.com/watch?v={video_id}"
        channel = entry.get("channel") or entry.get("uploader") or "unknown channel"
        thumbnails = entry.get("thumbnails") or []
        preview_url = thumbnails[-1].get("url") if thumbnails else None
        duration = entry.get("duration")
        orientation: Orientation | None = (
            "portrait" if "/shorts/" in (entry.get("url") or "") else None
        )
        extra: dict[str, Any] = {k: entry[k] for k in _EXTRA_KEYS if entry.get(k) is not None}
        extra["search_query"] = search_query
        return Asset(
            provider=self.id,
            id=video_id,
            kind=kind,
            title=title,
            source_url=source_url,
            preview_url=preview_url,
            license="unknown",
            attribution_text=(
                f'"{title}" by {channel} via YouTube ({source_url}). '
                "Third-party content; license unknown, check before use."
            ),
            commercial_ok=None,
            duration_s=float(duration) if duration is not None else None,
            orientation=orientation,
            extra=extra,
        )

    def resolve(self, asset: Asset) -> DownloadPlan:
        """Plan a yt-dlp download of the canonical watch URL: mp3 for audio kinds, else mp4."""
        ext = "mp3" if asset.kind in AUDIO_KINDS else "mp4"
        return DownloadPlan(
            asset=asset, method="ytdlp", url=asset.source_url, ext=ext, ytdlp_opts={}
        )
