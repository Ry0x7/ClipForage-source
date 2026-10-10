"""Pexels provider: stock video and photo search via the Pexels API v1.

Key required: set `CLIPFORAGE_PEXELS_KEY` or `[keys] pexels = "..."` in config.toml.
All results are under the Pexels License (free for commercial use, attribution appreciated);
downloads are resolved to a plain HTTP `DownloadPlan` of the chosen file.
"""

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

from clipforage import config, http
from clipforage.models import Asset, DownloadPlan, Kind, Orientation, SearchFilters
from clipforage.providers.base import ProviderError

VIDEOS_URL = "https://api.pexels.com/videos/search"
PHOTOS_URL = "https://api.pexels.com/v1/search"
MAX_PER_PAGE = 80
MAX_VIDEO_WIDTH = 1920
LICENSE = "Pexels License"


def _orientation(width: Any, height: Any) -> Orientation | None:
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return None
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def _pick_video_file(files: list[dict[str, Any]]) -> str | None:
    """Pick the widest mp4 no wider than 1920px, else the widest mp4, else any mp4 link."""
    mp4s = [f for f in files if f.get("file_type") == "video/mp4" and f.get("link")]
    sized = [f for f in mp4s if isinstance(f.get("width"), int)]
    capped = [f for f in sized if f["width"] <= MAX_VIDEO_WIDTH]
    for pool in (capped, sized):
        if pool:
            return str(max(pool, key=lambda f: f["width"])["link"])
    return str(mp4s[0]["link"]) if mp4s else None


def _pick_preview_file(files: list[dict[str, Any]]) -> str | None:
    """Pick the smallest-width "sd" mp4, else the smallest-width mp4 of any quality."""
    sized = [
        f
        for f in files
        if f.get("file_type") == "video/mp4" and f.get("link") and isinstance(f.get("width"), int)
    ]
    sd = [f for f in sized if f.get("quality") == "sd"]
    for pool in (sd, sized):
        if pool:
            return str(min(pool, key=lambda f: f["width"])["link"])
    return None


class PexelsProvider:
    """Searches Pexels videos and photos."""

    id = "pexels"
    kinds = frozenset({Kind.VIDEO, Kind.IMAGE})
    requires_key = True
    SEARCH_TTL_S = 3600

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        per_page: int = 15,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self.per_page = max(1, min(per_page, MAX_PER_PAGE))

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        """Return video or image assets; raise ProviderError on any failure."""
        if kind not in self.kinds:
            return []
        try:
            key = self._api_key or config.get_key(self.id)
            if not key:
                raise ProviderError(self.id, "missing API key (set CLIPFORAGE_PEXELS_KEY)")
            url = VIDEOS_URL if kind == Kind.VIDEO else PHOTOS_URL
            params: dict[str, str | int] = {"query": query, "per_page": self.per_page}
            if filters.orientation is not None:
                params["orientation"] = filters.orientation
            client = self._client if self._client is not None else http.get_client()
            response = client.get(url, params=params, headers={"Authorization": key})
            if not response.is_success:
                raise ProviderError(self.id, f"HTTP {response.status_code} from {url}")
            data = response.json()
            if kind == Kind.VIDEO:
                assets = [self._video_to_asset(hit) for hit in data.get("videos") or []]
            else:
                assets = [self._photo_to_asset(hit) for hit in data.get("photos") or []]
            return [a for a in assets if a is not None]
        except ProviderError:
            raise
        except httpx.HTTPError as e:
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from e
        except Exception as e:  # bad JSON, unexpected shape; never let anything else escape
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from e

    def _video_to_asset(self, hit: dict[str, Any]) -> Asset | None:
        video_id = str(hit["id"])
        user = hit.get("user") or {}
        name = user.get("name") or "unknown"
        width, height = hit.get("width"), hit.get("height")
        files = hit.get("video_files") or []
        download_url = _pick_video_file(files)
        if download_url is None:
            return None  # no usable mp4: skip this hit
        duration = hit.get("duration")
        media_url = _pick_preview_file(files)
        return Asset(
            provider=self.id,
            id=video_id,
            kind=Kind.VIDEO,
            title=f"Pexels video {video_id} by {name}",
            source_url=hit["url"],
            preview_url=hit.get("image"),
            media_url=media_url,
            media_type="video/mp4" if media_url else None,
            license=LICENSE,
            attribution_text=f"Video by {name} on Pexels ({user.get('url') or hit['url']})",
            commercial_ok=True,
            duration_s=float(duration) if duration is not None else None,
            orientation=_orientation(width, height),
            extra={"width": width, "height": height, "download_url": download_url},
        )

    def _photo_to_asset(self, hit: dict[str, Any]) -> Asset | None:
        photo_id = str(hit["id"])
        name = hit.get("photographer") or "unknown"
        width, height = hit.get("width"), hit.get("height")
        src = hit.get("src") or {}
        download_url = src.get("original")
        if not download_url:
            return None  # no src.original: skip this hit
        return Asset(
            provider=self.id,
            id=photo_id,
            kind=Kind.IMAGE,
            title=hit.get("alt") or f"Pexels photo {photo_id} by {name}",
            source_url=hit["url"],
            preview_url=src.get("medium"),
            license=LICENSE,
            attribution_text=(
                f"Photo by {name} on Pexels ({hit.get('photographer_url') or hit['url']})"
            ),
            commercial_ok=True,
            orientation=_orientation(width, height),
            extra={"width": width, "height": height, "download_url": download_url},
        )

    def resolve(self, asset: Asset) -> DownloadPlan:
        """Plan a plain HTTP download of the file chosen at search time."""
        url = asset.extra.get("download_url")
        if not url:
            raise ProviderError(self.id, f"asset {asset.id} has no download_url in extra")
        url = str(url)
        if asset.kind == Kind.VIDEO:
            ext = "mp4"
        else:
            ext = PurePosixPath(urlparse(url).path).suffix.lstrip(".").lower() or "jpg"
        return DownloadPlan(asset=asset, method="http", url=url, ext=ext, headers={})
