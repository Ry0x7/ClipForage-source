"""Core data models shared by providers, the registry, and the downloader."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from clipforage.providers.base import ProviderError


class Kind(StrEnum):
    """Asset kinds a provider can return."""

    VIDEO = "video"
    IMAGE = "image"
    GIF = "gif"
    STICKER = "sticker"
    AUDIO_SFX = "audio_sfx"
    AUDIO_MUSIC = "audio_music"
    FONT = "font"
    LUT = "lut"
    OVERLAY = "overlay"
    TEMPLATE = "template"


Orientation = Literal["landscape", "portrait", "square"]
Style = Literal["any", "meme", "green_screen", "cinematic"]
Quality = Literal["best", "2160", "1440", "1080", "720", "480", "360"]
"""yt-dlp video quality: a max height in pixels, or "best" (no cap)."""
QUALITIES: tuple[str, ...] = get_args(Quality)
SHORTS_MAX_DURATION_S = 60.0
"""Duration cap for ``filters.shorts`` (YouTube Shorts are at most 60 s)."""


class SearchFilters(BaseModel):
    """Filters applied to a search, partly by providers and partly client-side."""

    model_config = ConfigDict(frozen=True)

    kind: Kind
    commercial_only: bool = False
    cleared_only: bool = False
    """Only rights-cleared results (``rights.is_cleared``): providers may narrow their query to
    commercial-capable licenses; ``service.search`` still post-filters."""
    min_duration: float | None = None
    max_duration: float | None = None
    orientation: Orientation | None = None
    license: str | None = None
    style: Style = "any"
    """Editing intent; providers may use it to refine the query (e.g. youtube)."""
    limit: int | None = None
    """Requested result count; providers may use it to over-fetch before filtering."""
    expand: bool = True
    """``service.search_expanded``: also search offline recall expansions of the query."""
    related: bool = True
    """``service.search_expanded``: for audio kinds, also search the other audio kind."""
    safe: bool = True
    """Safe search: providers use native knobs and ``safesearch.is_explicit`` drops results."""
    shorts: bool = False
    """Shorts intent (video only): <= 60 s for every provider; youtube results first (their
    orientation is unknown), then other providers portrait first (``cinematic`` style keeps
    portrait first across providers); youtube also adds a " shorts" query term."""


class Asset(BaseModel):
    """A single search result from a provider."""

    model_config = ConfigDict(frozen=True)

    provider: str
    id: str
    kind: Kind
    title: str
    source_url: str
    preview_url: str | None = None
    """Thumbnail IMAGE URL (jpg/png/webp/gif), or None. Never audio or video."""
    media_url: str | None = None
    """Small directly playable preview (mp4/webm/mp3/ogg), never a full-res download."""
    media_type: str | None = None
    """MIME type of `media_url`, e.g. "video/mp4", "audio/mpeg", "audio/ogg", "video/webm"."""
    license: str
    """SPDX-ish license identifier, e.g. "CC0-1.0", or "unknown". Never None."""
    attribution_text: str
    commercial_ok: bool | None
    """True/False if known; None means unknown."""
    duration_s: float | None = None
    orientation: Orientation | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("license")
    @classmethod
    def _license_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('license must be non-empty; use "unknown" when not known')
        return value


class RightsLevel(StrEnum):
    """How freely an asset can be used, from a quick license check (see ``rights.classify``)."""

    CLEAR = "clear"
    ATTRIBUTION = "attribution"
    NONCOMMERCIAL = "noncommercial"
    COPYRIGHTED = "copyrighted"
    UNKNOWN = "unknown"


class Rights(BaseModel):
    """A UI-ready copyright signal for one asset."""

    model_config = ConfigDict(frozen=True)

    level: RightsLevel
    label: str
    """Short label, e.g. "Free to use (CC0 1.0)" or "Non-commercial only"."""
    detail: str
    """One sentence including what to do."""


@dataclass
class SearchOutcome:
    """Result of ``service.search_expanded``: main results plus recall and related audio."""

    assets: list[Asset]
    errors: "list[ProviderError]"
    expanded_queries: list[str] = field(default_factory=list)
    """Recall expansions that were actually searched, in order."""
    related: list[Asset] = field(default_factory=list)
    """Results of the other audio kind (deduped against ``assets``)."""
    related_kind: Kind | None = None
    filtered_count: int = 0
    """Distinct results (provider + id) hidden by safe search across all searched queries."""
    relaxed_query: str | None = None
    """Set when a cleared-only audio search found nothing and ``assets`` come from this
    filler-stripped query instead (``service.relax_query``)."""


class DownloadPlan(BaseModel):
    """How to fetch an asset: plain HTTP or via yt-dlp."""

    model_config = ConfigDict(frozen=True)

    asset: Asset
    method: Literal["http", "ytdlp"]
    url: str
    ext: str
    headers: dict[str, str] = Field(default_factory=dict)
    ytdlp_opts: dict[str, Any] = Field(default_factory=dict)


class DownloadResult(BaseModel):
    """Outcome of a completed download."""

    model_config = ConfigDict(frozen=True)

    path: Path
    sidecar: Path
    fetched_at: datetime


class ImportReport(BaseModel):
    """Outcome of an editor library import; entries are file paths as strings."""

    bin_name: str
    imported: list[str] = Field(default_factory=list)
    skipped_existing: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)


class Cue(BaseModel):
    """One subtitle/caption cue: text spoken between ``start_s`` and ``end_s`` (seconds)."""

    model_config = ConfigDict(frozen=True)

    start_s: float
    end_s: float
    text: str


class PhraseHit(BaseModel):
    """A phrase-search hit: the asset plus the matching caption cue and its timestamps."""

    model_config = ConfigDict(frozen=True)

    asset: Asset
    start_s: float
    end_s: float
    text: str
    lang: str | None = None
    auto_generated: bool = False

    @property
    def ref(self) -> str:
        """``<provider>:<id>`` ref usable with ``clipforage get``."""
        return f"{self.asset.provider}:{self.asset.id}"

    def clip_spec(self, pad_s: float = 1.0) -> str:
        """'START-END' in seconds with one decimal, start clamped at 0, both padded by pad_s.

        Parseable by ``download.service.parse_clip`` (end is always after start).
        """
        start = round(max(0.0, self.start_s - pad_s), 1)
        end = round(max(self.end_s + pad_s, 0.0), 1)
        if end <= start:
            end = round(start + 0.1, 1)
        return f"{start:.1f}-{end:.1f}"


@dataclass
class PhraseOutcome:
    """Result of ``service.phrase_search``: hits, per-source errors and the safe-search count."""

    hits: list[PhraseHit]
    errors: "list[ProviderError]"
    filtered_count: int = 0
    """Distinct results (provider + id) hidden by safe search (candidates and indexed hits)."""


class AssistResult(BaseModel):
    """LLM search assist: a free-text description turned into concrete search queries."""

    queries: list[str]
    """2-6 concrete search strings, best first."""
    names: list[str] = Field(default_factory=list)
    """Canonical meme / track / template names (may be empty)."""
    kind_hint: Kind | None = None
    notes: str = ""
    """One or two sentences: what the thing is, why these queries."""
    rights_note: str | None = None
    """Set when the thing is commercial copyrighted material, with cleared alternatives."""
    backend: str
    model: str | None = None
    cost_usd: float | None = None
