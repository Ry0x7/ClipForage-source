"""Request and response models for the local web API."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from clipforage.linkout import LinkOut
from clipforage.models import Asset, Quality, Rights

JobStatus = Literal["queued", "running", "done", "error"]


class ProviderInfo(BaseModel):
    """One provider as returned by ``service.list_providers()``."""

    id: str
    kinds: list[str]
    requires_key: bool
    has_key: bool


class RightsOut(Rights):
    """The rights / copyright signal of an asset (``rights.classify``)."""


class AssetOut(Asset):
    """A search result plus its ``<provider>:<id>`` ref and rights signal.

    ``history_id`` / ``history_path`` name the newest download of this asset whose file still
    exists in the library (set at response time, never cached); the UI uses them for drag-out.
    """

    ref: str
    rights: RightsOut
    history_id: int | None = None
    history_path: str | None = None


class SearchErrorOut(BaseModel):
    """A single provider's failure during a search."""

    provider: str
    message: str


class SearchResponse(BaseModel):
    """Search results, per-provider errors, and link-out sites for the kind."""

    assets: list[AssetOut]
    errors: list[SearchErrorOut]
    linkouts: list[LinkOut]
    expanded_queries: list[str] = Field(default_factory=list)
    """Offline recall expansions that were also searched."""
    related: list[AssetOut] = Field(default_factory=list)
    """Results of the related audio kind (music <-> sfx), deduped against ``assets``."""
    related_kind: str | None = None
    filtered_count: int = 0
    """Results hidden by safe search (``safe``); the UI may show "N hidden by safe search"."""
    relaxed_query: str | None = None
    """Cleared-only audio search found nothing; ``assets`` are for this filler-stripped query."""


class RecallResponse(BaseModel):
    """Offline "did you mean" meme names for a description."""

    suggestions: list[str]


class PhraseHitOut(BaseModel):
    """A phrase-search hit: the asset, the matching cue, its ref and a padded clip range."""

    asset: AssetOut
    start_s: float
    end_s: float
    text: str
    lang: str | None
    auto_generated: bool
    ref: str
    clip: str


class PhraseResponse(BaseModel):
    """Phrase hits, per-source errors, and the PlayPhrase link-out."""

    hits: list[PhraseHitOut]
    errors: list[SearchErrorOut]
    linkouts: list[LinkOut]
    filtered_count: int = 0
    """Results (provider + id) hidden by safe search (``safe``), as in ``SearchResponse``."""


class DownloadRequest(BaseModel):
    """Download a cached ``ref`` or a yt-dlp ``url`` (exactly one of them)."""

    ref: str | None = None
    url: str | None = None
    mp3: bool = False
    clip: str | None = None
    max_wall_s: float | None = Field(default=None, ge=0)
    """Wall-clock limit for an http ``ref`` clip re-encode; 0 = no limit, None = default."""
    quality: Quality | None = None
    """Max video height for yt-dlp downloads; None = ``[download] quality`` or best."""
    title: str | None = None
    """Display title for the job (used for ``url`` downloads; a ``ref`` uses the asset title)."""


class KeyOutRequest(BaseModel):
    """Key out the green screen of a library video into a transparent WebM."""

    tolerance: int = Field(default=80, ge=0, le=120)
    """How much green to key out (higher keys more); same scale as the preview slider."""
    max_wall_s: float | None = Field(default=None, ge=0)
    """Wall-clock limit for the ffmpeg re-encode; 0 = no limit, None = default."""


class DownloadAccepted(BaseModel):
    """A queued download job."""

    job_id: str
    status: JobStatus


class JobOut(BaseModel):
    """The state of a download job."""

    job_id: str
    kind: str = "download"
    """``download`` or ``keyout``."""
    ref: str | None
    url: str | None
    status: JobStatus
    progress: float | None
    downloaded_bytes: int | None
    total_bytes: int | None
    path: str | None
    sidecar: str | None
    license: str | None
    attribution_text: str | None
    error: str | None
    history_id: int | None = None
    stage: str | None = None
    """``postprocessing`` while yt-dlp merges / extracts audio after the download; else None."""
    title: str | None = None
    """Asset title when known at job creation (``ref`` asset title or the request ``title``)."""
    created_at: str


class HistoryRow(BaseModel):
    """One download history row from cache.db."""

    id: int
    url: str
    path: str
    sidecar: str
    provider: str
    asset_id: str
    title: str
    license: str
    fetched_at: str
    extra: dict[str, Any]


class AssistKeysOut(BaseModel):
    """Masked assist API keys (None when unset)."""

    anthropic: str | None
    openai: str | None


class AssistSettingsOut(BaseModel):
    """``[assist]`` settings: off by default; ``model`` None means the backend default."""

    enabled: bool
    backend: str
    model: str | None
    claude_cli_model: str | None = None
    anthropic_model: str | None = None
    openai_model: str | None = None
    """Per-backend models (``[assist] <backend>_model``); None falls back to ``model``."""
    keys: AssistKeysOut


class SearchSettingsOut(BaseModel):
    """``[search]`` settings: ``safe`` is the safe search default (on unless turned off)."""

    safe: bool


class SettingsOut(BaseModel):
    """Library folder, config file path, masked keys and providers. Never raw keys."""

    library_dir: str
    config_path: str
    keys: dict[str, str | None]
    providers: list[ProviderInfo]
    qualities: list[str]
    """Allowed ``DownloadRequest.quality`` values."""
    assist: AssistSettingsOut
    search: SearchSettingsOut


class SearchSettingsIn(BaseModel):
    """Update ``[search]``: ``safe`` true/false sets it; None removes the key (default on)."""

    safe: bool | None = None


class AssistSettingsIn(BaseModel):
    """Update ``[assist]``: None leaves a field unchanged; an empty ``model`` (or
    ``<backend>_model``) clears it and ``backend="auto"`` removes the key (auto is the default)."""

    enabled: bool | None = None
    backend: str | None = None
    model: str | None = None
    claude_cli_model: str | None = None
    anthropic_model: str | None = None
    openai_model: str | None = None


class AssistIn(BaseModel):
    """A free-text description to turn into search queries (only on explicit user request)."""

    text: str
    kind: str | None = None
    style: str | None = None
    backend: str | None = None


class AssistOut(BaseModel):
    """Search queries, names and a rights note suggested by the assist backend."""

    queries: list[str]
    names: list[str]
    kind_hint: str | None
    notes: str
    rights_note: str | None
    backend: str
    model: str | None
    cost_usd: float | None


class AssistBackendOut(BaseModel):
    """One assist backend's availability."""

    id: str
    available: bool
    detail: str
    model: str | None


class AssistStatusOut(BaseModel):
    """Every assist backend, the configured choice and what ``auto`` would pick now."""

    backends: list[AssistBackendOut]
    configured: str
    auto_pick: str | None
    enabled: bool


class KeyIn(BaseModel):
    """An API key; the empty string removes it."""

    key: str


class KeyOut(BaseModel):
    """The masked key after an update (None when removed or unset)."""

    provider: str
    key: str | None


class LibraryDirIn(BaseModel):
    """A new absolute library folder."""

    dir: str


class LibraryDirOut(BaseModel):
    """The library folder after an update."""

    library_dir: str


class ToolInfo(BaseModel):
    """Whether a binary was found, and where."""

    found: bool
    path: str | None


class YtDlpInfo(BaseModel):
    """The installed yt-dlp version."""

    version: str


class DoctorOut(BaseModel):
    """ffmpeg / Deno / yt-dlp / yt-dlp-ejs status."""

    ffmpeg: ToolInfo
    deno: ToolInfo
    yt_dlp: YtDlpInfo
    ejs: bool


class OpenIn(BaseModel):
    """A path inside the library folder to show in the OS file manager."""

    path: str


class OpenOut(BaseModel):
    """The folder that was opened."""

    opened: str
