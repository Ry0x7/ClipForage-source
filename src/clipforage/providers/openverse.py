"""Openverse provider: openly licensed images and audio via the Openverse API.

No key required. Optionally set `CLIPFORAGE_OPENVERSE_KEY` (or `[keys] openverse = "..."` in
config.toml) to an Openverse API token; it is then sent as `Authorization: Bearer <token>`
to api.openverse.org, which raises the anonymous rate and page-size limits.
Licenses vary per result (CC BY, CC BY-NC-SA, CC0, Public Domain Mark, ...); NC licenses are
flagged `commercial_ok=False`. With ``SearchFilters.cleared_only`` the request is narrowed to
``license=cc0,pdm,by,by-sa`` (plus ``license_type=commercial``).
"""

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

from clipforage import config, http
from clipforage.models import Asset, DownloadPlan, Kind, Orientation, SearchFilters
from clipforage.providers.base import ProviderError

IMAGES_URL = "https://api.openverse.org/v1/images/"
AUDIO_URL = "https://api.openverse.org/v1/audio/"
MAX_PAGE_SIZE_ANON = 20  # anonymous requests are capped at 20 results per page
MAX_PAGE_SIZE_AUTH = 500  # with an API token
CLEARED_LICENSES = "cc0,pdm,by,by-sa"  # `license` codes that rights.py classifies as cleared
_ASPECT_RATIO_PARAM: dict[str, str] = {"landscape": "wide", "portrait": "tall", "square": "square"}
_AUDIO_CATEGORY: dict[Kind, str] = {Kind.AUDIO_SFX: "sound_effect", Kind.AUDIO_MUSIC: "music"}
_AUDIO_MIME: dict[str, str] = {
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
}


def _orientation(width: Any, height: Any) -> Orientation | None:
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return None
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def _license(code: Any, version: Any) -> str:
    code = str(code or "").strip()
    if not code:
        return "unknown"
    if code.lower() == "cc0":
        return "CC0 1.0"
    if code.lower() == "pdm":
        return "Public Domain Mark"
    return f"CC {code.upper()} {str(version or '').strip()}".strip()


class OpenverseProvider:
    """Searches Openverse images and audio (sound effects and music)."""

    id = "openverse"
    kinds = frozenset({Kind.IMAGE, Kind.AUDIO_SFX, Kind.AUDIO_MUSIC})
    requires_key = False
    SEARCH_TTL_S = 3600

    def __init__(
        self,
        client: httpx.Client | None = None,
        page_size: int = 20,
        api_key: str | None = None,
    ) -> None:
        self._client = client
        self.page_size = max(1, page_size)
        self._api_key = api_key

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        """Return assets for `kind`; raise ProviderError on any failure."""
        if kind not in self.kinds:
            return []
        key: str | None = None
        try:
            key = self._api_key or config.get_key(self.id)
            cap = MAX_PAGE_SIZE_AUTH if key else MAX_PAGE_SIZE_ANON
            params: dict[str, str | int] = {"q": query, "page_size": min(self.page_size, cap)}
            if kind == Kind.IMAGE:
                url = IMAGES_URL
                if filters.orientation in _ASPECT_RATIO_PARAM:
                    params["aspect_ratio"] = _ASPECT_RATIO_PARAM[filters.orientation]
            else:
                url = AUDIO_URL
                params["category"] = _AUDIO_CATEGORY[kind]
            if filters.commercial_only or filters.cleared_only:
                params["license_type"] = "commercial"
            if filters.cleared_only:
                params["license"] = CLEARED_LICENSES
            if filters.safe:
                params["mature"] = "false"
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            client = self._client if self._client is not None else http.get_client()
            response = client.get(url, params=params, headers=headers)
            if not response.is_success:
                body = response.text.strip()[:200]
                raise ProviderError(self.id, f"HTTP {response.status_code}: {body}")
            data = response.json()
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise ProviderError(self.id, "unexpected response: no 'results' list")
            assets: list[Asset] = []
            for hit in results:
                if not isinstance(hit, dict) or not hit.get("id"):
                    continue
                if not hit.get("url") or not hit.get("foreign_landing_url"):
                    continue
                if filters.safe and hit.get("mature") is True:
                    continue
                asset = self._asset(hit, kind)
                if kind != Kind.IMAGE and asset.duration_s is not None:
                    if filters.min_duration is not None and asset.duration_s < filters.min_duration:
                        continue
                    if filters.max_duration is not None and asset.duration_s > filters.max_duration:
                        continue
                assets.append(asset)
            return assets
        except ProviderError as e:
            raise ProviderError(self.id, _scrub(e.message, key)) from None
        except Exception as e:  # noqa: BLE001 - never let any other exception escape search
            raise ProviderError(self.id, _scrub(f"{type(e).__name__}: {e}", key)) from None

    def _asset(self, hit: dict[str, Any], kind: Kind) -> Asset:
        license_str = _license(hit.get("license"), hit.get("license_version"))
        title = str(hit.get("title") or "").strip() or f"Openverse {kind.value} {hit['id']}"
        creator = hit.get("creator") or "unknown creator"
        attribution = str(hit.get("attribution") or "").strip()
        if not attribution:
            attribution = f"{title} by {creator} ({license_str})"
        commercial_ok = None if license_str == "unknown" else "nc" not in license_str.lower()
        duration_s: float | None = None
        orientation: Orientation | None = None
        media_url: str | None = None
        media_type: str | None = None
        if kind == Kind.IMAGE:
            orientation = _orientation(hit.get("width"), hit.get("height"))
        else:
            duration = hit.get("duration")
            if isinstance(duration, int | float) and not isinstance(duration, bool):
                duration_s = duration / 1000
            media_url = str(hit["url"])
            media_type = _AUDIO_MIME.get(str(hit.get("filetype") or "").strip().lower())
        return Asset(
            provider=self.id,
            id=str(hit["id"]),
            kind=kind,
            title=title,
            source_url=str(hit["foreign_landing_url"]),
            preview_url=hit.get("thumbnail") or None,
            media_url=media_url,
            media_type=media_type,
            license=license_str,
            attribution_text=attribution,
            commercial_ok=commercial_ok,
            duration_s=duration_s,
            orientation=orientation,
            extra={
                "download_url": hit["url"],
                "filetype": hit.get("filetype"),
                "license_url": hit.get("license_url"),
                "creator": hit.get("creator"),
                "creator_url": hit.get("creator_url"),
                "provider": hit.get("provider"),
            },
        )

    def resolve(self, asset: Asset) -> DownloadPlan:
        """Plan a plain HTTP download of the original file."""
        url = asset.extra.get("download_url")
        if not url:
            raise ProviderError(self.id, f"asset {asset.id} has no download_url")
        ext = str(asset.extra.get("filetype") or "").strip().lstrip(".").lower()
        if not ext:
            ext = PurePosixPath(urlparse(url).path).suffix.lstrip(".").lower()
        if not ext:
            ext = "jpg" if asset.kind == Kind.IMAGE else "mp3"
        return DownloadPlan(asset=asset, method="http", url=url, ext=ext)


def _scrub(message: str, key: str | None) -> str:
    """Remove the API token from an error message."""
    return message.replace(key, "***") if key else message
