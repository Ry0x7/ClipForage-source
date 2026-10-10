"""Internet Archive provider: public-domain and CC video, audio and images via archive.org.

No key required. Search uses the Advanced Search API (`advancedsearch.php`, JSON output);
`resolve` reads the item's file list from the Metadata API and plans a plain HTTP download
from `https://archive.org/download/<identifier>/<file>`. The license comes from each item's
`licenseurl`; items without one are reported as license "unknown" (commercial_ok None).

With safe search (``SearchFilters.safe``) the query excludes the adult collections and explicit
subjects listed in ``data/safesearch_terms.json``. With ``SearchFilters.cleared_only`` audio
queries keep only items with a non-NC ``licenseurl`` (``CLEARED_CLAUSE``); ``subject``, ``collection`` (minus users'
``fav-*`` lists) and a short ``description`` are kept in ``Asset.extra`` for the shared filter.
"""

import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from clipforage import http, safesearch
from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers.base import ProviderError

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DETAILS_URL = "https://archive.org/details/{identifier}"
PREVIEW_URL = "https://archive.org/services/img/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"
FIELDS = (
    "identifier",
    "title",
    "mediatype",
    "licenseurl",
    "creator",
    "description",
    "date",
    "downloads",
    "subject",
    "collection",
)
_DESCRIPTION_MAX = 500
CLEARED_CLAUSE = " AND licenseurl:* AND NOT licenseurl:*nc*"
"""``SearchFilters.cleared_only`` for audio: only items with a license URL that is not CC NC."""
_MEDIATYPE: dict[Kind, str] = {
    Kind.VIDEO: "movies",
    Kind.AUDIO_MUSIC: "audio",
    Kind.AUDIO_SFX: "audio",
    Kind.IMAGE: "image",
}
_VIDEO_FALLBACK_EXTS = (".mp4", ".webm", ".ogv")
_AUDIO_ORIGINAL_EXTS = (".mp3", ".ogg", ".flac", ".wav")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png")
# Path segment after /licenses/ -> (label, commercial use allowed).
_CC_LICENSES: dict[str, tuple[str, bool]] = {
    "by": ("CC BY", True),
    "by-sa": ("CC BY-SA", True),
    "by-nd": ("CC BY-ND", True),
    "by-nc": ("CC BY-NC", False),
    "by-nc-sa": ("CC BY-NC-SA", False),
    "by-nc-nd": ("CC BY-NC-ND", False),
}
_CC_RE = re.compile(r"creativecommons\.org/licenses/([a-z-]+)(?:/([0-9.]+))?", re.IGNORECASE)


def license_from_url(url: str | None) -> tuple[str, bool | None]:
    """Map an archive.org `licenseurl` to (label, commercial_ok)."""
    if not url:
        return "unknown", None
    lowered = url.lower()
    if "creativecommons.org/publicdomain/zero" in lowered:
        return "CC0 1.0", True
    if "creativecommons.org/publicdomain/mark" in lowered:
        return "Public Domain Mark", True
    match = _CC_RE.search(url)
    if match and match.group(1).lower() in _CC_LICENSES:
        label, commercial = _CC_LICENSES[match.group(1).lower()]
        version = match.group(2)
        return (f"{label} {version}" if version else label), commercial
    return "unknown", None


def _text(value: Any) -> str:
    """Advanced Search fields may be a string or a list of strings."""
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip() if value is not None else ""


def _strings(value: Any) -> list[str]:
    """A string-or-list Advanced Search field as a list of non-empty strings."""
    values = value if isinstance(value, list) else [value] if value is not None else []
    return [str(v).strip() for v in values if str(v).strip()]


def safe_clause() -> str:
    """`` AND NOT collection:(...) AND NOT subject:(...)`` excluding adult items."""
    collections = " OR ".join(f'"{c}"' for c in safesearch.ia_collections())
    subjects = " OR ".join(safesearch.ia_subjects())
    clause = ""
    if collections:
        clause += f" AND NOT collection:({collections})"
    if subjects:
        clause += f" AND NOT subject:({subjects})"
    return clause


def _size(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def _length_seconds(value: Any) -> float | None:
    """Parse a Metadata API `length` ("12.34" seconds or "hh:mm:ss")."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        if ":" in text:
            seconds = 0.0
            for part in text.split(":"):
                seconds = seconds * 60 + float(part)
            return seconds
        return float(text)
    except ValueError:
        return None


class InternetArchiveProvider:
    """Searches Internet Archive movies, audio and images (sorted by downloads)."""

    id = "internet_archive"
    kinds = frozenset({Kind.VIDEO, Kind.AUDIO_MUSIC, Kind.AUDIO_SFX, Kind.IMAGE})
    requires_key = False
    SEARCH_TTL_S = 86400
    cache_empty_results = True

    def __init__(self, client: httpx.Client | None = None, rows: int = 20) -> None:
        self._client = client
        self.rows = max(1, rows)

    def _get(self, url: str, params: list[tuple[str, str | int]] | None = None) -> Any:
        client = self._client if self._client is not None else http.get_client()
        response = client.get(url, params=params)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            hint = f" (Retry-After: {retry_after})" if retry_after else ""
            raise ProviderError(self.id, f"HTTP 429: rate limited{hint}")
        if not response.is_success:
            body = response.text.strip()[:200]
            raise ProviderError(self.id, f"HTTP {response.status_code}: {body}")
        return response.json()

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        """Return assets for `kind`; raise ProviderError on any failure."""
        if kind not in self.kinds:
            return []
        try:
            params: list[tuple[str, str | int]] = [
                ("q", self.query(query, kind, safe=filters.safe, cleared=filters.cleared_only)),
                *(("fl[]", field) for field in FIELDS),
                ("rows", self.rows),
                ("page", 1),
                ("sort[]", "downloads desc"),
                ("output", "json"),
            ]
            data = self._get(SEARCH_URL, params)
            response = data.get("response") if isinstance(data, dict) else None
            docs = response.get("docs") if isinstance(response, dict) else None
            if not isinstance(docs, list):
                raise ProviderError(self.id, "unexpected response: no 'response.docs' list")
            return [
                self._asset(doc, kind)
                for doc in docs
                if isinstance(doc, dict) and _text(doc.get("identifier"))
            ]
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - never let any other exception escape search
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from None

    def query(self, query: str, kind: Kind, *, safe: bool, cleared: bool = False) -> str:
        """The Advanced Search ``q`` for ``query`` in ``kind``, with the safe clause if ``safe``.

        ``cleared`` (audio kinds only) keeps items with a non-NC ``licenseurl`` (CLEARED_CLAUSE).
        """
        q = f"({query}) AND mediatype:({_MEDIATYPE[kind]})"
        if cleared and kind in (Kind.AUDIO_MUSIC, Kind.AUDIO_SFX):
            q += CLEARED_CLAUSE
        return q + safe_clause() if safe else q

    def search_with_subtitles(self, query: str, rows: int) -> list[Asset]:
        """Return movie items matching ``query`` that ship SubRip or WebVTT subtitle files.

        An empty ``query`` returns the most-downloaded ``collection:feature_films`` items with
        subtitles. Used by phrase search; raises ProviderError on any failure.
        """
        subtitles = '(format:"SubRip" OR format:"Web Video Text Tracks")'
        if query.strip():
            q = f"({query}) AND mediatype:movies AND {subtitles}"
        else:
            q = f"mediatype:movies AND collection:feature_films AND {subtitles}"
        try:
            params: list[tuple[str, str | int]] = [
                ("q", q),
                *(("fl[]", field) for field in FIELDS),
                ("rows", max(1, rows)),
                ("page", 1),
                ("sort[]", "downloads desc"),
                ("output", "json"),
            ]
            data = self._get(SEARCH_URL, params)
            response = data.get("response") if isinstance(data, dict) else None
            docs = response.get("docs") if isinstance(response, dict) else None
            if not isinstance(docs, list):
                raise ProviderError(self.id, "unexpected response: no 'response.docs' list")
            return [
                self._asset(doc, Kind.VIDEO)
                for doc in docs
                if isinstance(doc, dict) and _text(doc.get("identifier"))
            ]
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - never let any other exception escape
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from None

    def _asset(self, doc: dict[str, Any], kind: Kind) -> Asset:
        identifier = _text(doc.get("identifier"))
        title = _text(doc.get("title")) or identifier
        creator = _text(doc.get("creator")) or "unknown"
        license_url = _text(doc.get("licenseurl")) or None
        label, commercial_ok = license_from_url(license_url)
        return Asset(
            provider=self.id,
            id=identifier,
            kind=kind,
            title=title,
            source_url=DETAILS_URL.format(identifier=identifier),
            preview_url=PREVIEW_URL.format(identifier=identifier),
            license=label,
            attribution_text=f"{title} by {creator}, Internet Archive ({identifier})",
            commercial_ok=commercial_ok,
            duration_s=None,
            orientation=None,
            extra={
                "identifier": identifier,
                "mediatype": _text(doc.get("mediatype")) or None,
                "license_url": license_url,
                "subject": _strings(doc.get("subject")),
                "collection": [
                    c for c in _strings(doc.get("collection")) if not c.startswith("fav-")
                ],
                "description": _text(doc.get("description"))[:_DESCRIPTION_MAX] or None,
            },
        )

    def resolve(self, asset: Asset) -> DownloadPlan:
        """Pick a downloadable file from the item's metadata and plan an HTTP download."""
        identifier = str(asset.extra.get("identifier") or asset.id)
        try:
            data = self._get(METADATA_URL.format(identifier=quote(identifier, safe="")))
            files = data.get("files") if isinstance(data, dict) else None
            if not isinstance(files, list):
                raise ProviderError(self.id, f"item {identifier} has no 'files' list")
            entries = [f for f in files if isinstance(f, dict) and f.get("name")]
            chosen = self._pick(entries, asset.kind)
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - wrap httpx/JSON errors
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from None
        if chosen is None:
            raise ProviderError(self.id, f"item {identifier} has no suitable {asset.kind} file")
        filename = str(chosen["name"])
        ext = PurePosixPath(filename).suffix.lstrip(".").lower()
        duration = _length_seconds(chosen.get("length"))
        if duration is not None:
            asset = asset.model_copy(update={"duration_s": duration})
        url = DOWNLOAD_URL.format(identifier=quote(identifier, safe=""), filename=quote(filename))
        return DownloadPlan(asset=asset, method="http", url=url, ext=ext)

    def _pick(self, entries: list[dict[str, Any]], kind: Kind) -> dict[str, Any] | None:
        def name(entry: dict[str, Any]) -> str:
            return str(entry["name"]).lower()

        def largest(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
            return max(candidates, key=_size) if candidates else None

        if kind == Kind.VIDEO:
            mp4s = [e for e in entries if name(e).endswith(".mp4")]
            return (
                largest([e for e in mp4s if e.get("source") == "original"])
                or largest(mp4s)
                or largest([e for e in entries if name(e).endswith(_VIDEO_FALLBACK_EXTS)])
            )
        if kind in (Kind.AUDIO_MUSIC, Kind.AUDIO_SFX):
            # original mp3 > VBR MP3 > any mp3 (e.g. "128Kbps MP3") > other original audio
            mp3s = [e for e in entries if name(e).endswith(".mp3")]
            originals = [
                e
                for e in entries
                if e.get("source") == "original" and name(e).endswith(_AUDIO_ORIGINAL_EXTS)
            ]
            return (
                largest([e for e in mp3s if e.get("source") == "original"])
                or largest([e for e in mp3s if e.get("format") == "VBR MP3"])
                or largest(mp3s)
                or largest(originals)
            )
        if kind == Kind.IMAGE:
            originals = [
                e
                for e in entries
                if e.get("source") == "original" and name(e).endswith(_IMAGE_EXTS)
            ]
            return largest(originals)
        return None
