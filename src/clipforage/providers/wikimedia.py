"""Wikimedia Commons provider: images, GIFs, video and audio via the MediaWiki Action API.

No key required. Wikimedia requires a descriptive User-Agent with contact info on every
request, so this provider sends its own `User-Agent` header (see `USER_AGENT`).
Licenses vary per file (CC BY, CC BY-SA, public domain, ...) and are read from extmetadata.
"""

import html
import re
import time
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

from clipforage import __version__, http
from clipforage.models import Asset, DownloadPlan, Kind, Orientation, SearchFilters
from clipforage.providers.base import ProviderError

API_URL = "https://commons.wikimedia.org/w/api.php"
# Wikimedia blocks generic User-Agents; the UA must identify the tool and give contact info.
# https://meta.wikimedia.org/wiki/User-Agent_policy
CONTACT = "https://github.com/Ry0x7/ClipForage-source/issues"
USER_AGENT = f"clipforage/{__version__} ({CONTACT}) httpx"
MAXLAG_RETRY_DELAY_S = 1.0

_FILETYPE: dict[Kind, str] = {
    Kind.IMAGE: "filetype:bitmap",
    Kind.GIF: "filemime:image/gif",
    Kind.VIDEO: "filetype:video",
    Kind.AUDIO_SFX: "filetype:audio",
    Kind.AUDIO_MUSIC: "filetype:audio",
}
_DROPPED_MIMES = frozenset(
    {"image/tiff", "image/svg+xml", "application/pdf", "image/vnd.djvu", "image/x-djvu"}
)
# Commons reports Ogg Vorbis/Theora files as "application/ogg" rather than audio/* or video/*.
_OGG_MIME = "application/ogg"
# Fallback MIME by file extension when imageinfo carries no `mime`.
_EXT_MIME: dict[str, str] = {
    "webm": "video/webm",
    "ogv": "video/ogg",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "mp4": "video/mp4",
}
_PLAYABLE_KINDS = frozenset({Kind.VIDEO, Kind.AUDIO_SFX, Kind.AUDIO_MUSIC})
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(value: str) -> str:
    """Remove HTML tags, unescape entities and collapse whitespace."""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", value))).strip()


def _orientation(width: Any, height: Any) -> Orientation | None:
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return None
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def _mime_matches(kind: Kind, mime: str) -> bool:
    if not mime or mime in _DROPPED_MIMES:
        return False
    if kind == Kind.GIF:
        return mime == "image/gif"
    if kind == Kind.IMAGE:
        return mime.startswith("image/") and mime != "image/gif"
    if kind == Kind.VIDEO:
        return mime.startswith("video/") or mime == _OGG_MIME
    return mime.startswith("audio/") or mime == _OGG_MIME


def _meta(extmetadata: dict[str, Any], name: str) -> str | None:
    entry = extmetadata.get(name)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _commercial_ok(license_short: str | None) -> bool | None:
    if license_short is None:
        return None
    return "NC" not in license_short and license_short.lower() not in {"fair use", "unknown"}


class WikimediaProvider:
    """Searches Wikimedia Commons files (namespace 6) with license metadata."""

    id = "wikimedia"
    kinds = frozenset({Kind.IMAGE, Kind.VIDEO, Kind.AUDIO_SFX, Kind.AUDIO_MUSIC, Kind.GIF})
    requires_key = False
    SEARCH_TTL_S = 3600

    def __init__(
        self,
        client: httpx.Client | None = None,
        limit: int = 20,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self.limit = max(1, min(500, limit))
        self._sleep = sleep

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        """Return assets for `kind`; raise ProviderError on any failure."""
        if kind not in self.kinds:
            return []
        try:
            params: dict[str, str | int] = {
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "generator": "search",
                "gsrsearch": f"{query} {_FILETYPE[kind]}",
                "gsrnamespace": 6,
                "gsrlimit": self.limit,
                "prop": "imageinfo",
                "iiprop": "url|extmetadata|mime|size",
                "iiurlwidth": 320,
                "maxlag": 5,
            }
            data = self._request(params)
            query_obj = data.get("query")
            if query_obj is None:
                return []  # no hits: MediaWiki omits "query" entirely
            pages = query_obj.get("pages") if isinstance(query_obj, dict) else None
            if not isinstance(pages, list):
                raise ProviderError(self.id, "unexpected response: no 'query.pages' list")
            assets: list[Asset] = []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                asset = self._asset(page, kind)
                if asset is not None:
                    assets.append(asset)
            return assets
        except ProviderError:
            raise
        except Exception as e:  # never let any other exception escape search
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from e

    def _request(self, params: dict[str, str | int]) -> dict[str, Any]:
        """GET the API, retrying once after a `maxlag` error."""
        client = self._client if self._client is not None else http.get_client()
        for attempt in range(2):
            response = client.get(API_URL, params=params, headers={"User-Agent": USER_AGENT})
            if not response.is_success:
                body = response.text.strip()[:200]
                raise ProviderError(self.id, f"HTTP {response.status_code}: {body}")
            data = response.json()
            if not isinstance(data, dict):
                raise ProviderError(self.id, "unexpected response: not a JSON object")
            error = data.get("error")
            if error is None:
                return data
            code = error.get("code") if isinstance(error, dict) else None
            info = error.get("info") if isinstance(error, dict) else error
            if code == "maxlag" and attempt == 0:
                self._sleep(MAXLAG_RETRY_DELAY_S)
                continue
            raise ProviderError(self.id, f"API error {code}: {info}")
        raise ProviderError(self.id, "unreachable")  # pragma: no cover

    def _asset(self, page: dict[str, Any], kind: Kind) -> Asset | None:
        infos = page.get("imageinfo")
        if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
            return None
        info = infos[0]
        mime = str(info.get("mime") or "")
        download_url = info.get("url")
        if page.get("pageid") is None or not download_url or not _mime_matches(kind, mime):
            return None
        extmetadata = info.get("extmetadata")
        if not isinstance(extmetadata, dict):
            extmetadata = {}
        license_short = _meta(extmetadata, "LicenseShortName")
        if license_short is not None:
            license_short = _strip_html(license_short) or None
        license_name = license_short or "unknown"
        artist = _meta(extmetadata, "Artist") or _meta(extmetadata, "Credit")
        author = _strip_html(artist) if artist else ""
        author = author or "Unknown author"
        attribution = _strip_html(f"{author} / Wikimedia Commons, {license_name}")
        attribution_required = _meta(extmetadata, "AttributionRequired")
        title = str(page.get("title") or "")
        title = title.removeprefix("File:")
        duration = info.get("duration")
        media_url: str | None = None
        media_type: str | None = None
        if kind in _PLAYABLE_KINDS:
            media_url = str(download_url)
            suffix = PurePosixPath(urlparse(media_url).path).suffix.lstrip(".").lower()
            media_type = mime or _EXT_MIME.get(suffix)
        return Asset(
            provider=self.id,
            id=str(page["pageid"]),
            kind=kind,
            title=title,
            source_url=str(info.get("descriptionurl") or ""),
            preview_url=info.get("thumburl"),
            media_url=media_url,
            media_type=media_type,
            license=license_name,
            attribution_text=attribution,
            commercial_ok=_commercial_ok(license_short),
            duration_s=float(duration) if isinstance(duration, int | float) else None,
            orientation=_orientation(info.get("width"), info.get("height")),
            extra={
                "download_url": download_url,
                "mime": mime,
                "license_url": _meta(extmetadata, "LicenseUrl"),
                # Commons categories (pipe-separated), read by the safe search filter.
                "categories": [c for c in (_meta(extmetadata, "Categories") or "").split("|") if c],
                "attribution_required": (
                    attribution_required.lower() == "true"
                    if attribution_required is not None
                    else None
                ),
            },
        )

    def resolve(self, asset: Asset) -> DownloadPlan:
        """Plan a plain HTTP download of the original file.

        The extension is taken as-is from the file name (webm/ogv/ogg/oga/jpg/png/gif/...);
        converting Commons' open formats to mp4/mp3 downstream is Later.
        """
        url = asset.extra.get("download_url")
        if not url:
            raise ProviderError(self.id, f"asset {asset.id} has no download_url")
        suffix = PurePosixPath(urlparse(url).path).suffix.lstrip(".").lower()
        return DownloadPlan(asset=asset, method="http", url=url, ext=suffix or "bin")
