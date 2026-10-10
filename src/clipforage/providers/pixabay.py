"""Pixabay provider: stock videos and images via the Pixabay API.

Key required: set `CLIPFORAGE_PIXABAY_KEY` or `[keys] pixabay = "..."` in config.toml.
All results are under the Pixabay Content License (commercial use allowed, attribution
appreciated). Pixabay's API terms require search results to be cached for 24 hours.
The key is only ever sent as the `key` query param to pixabay.com.
"""

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

from clipforage import config, http
from clipforage.models import Asset, DownloadPlan, Kind, Orientation, SearchFilters
from clipforage.providers.base import ProviderError

VIDEOS_URL = "https://pixabay.com/api/videos/"
IMAGES_URL = "https://pixabay.com/api/"
LICENSE = "Pixabay Content License"
_ORIENTATION_PARAM: dict[str, str] = {"landscape": "horizontal", "portrait": "vertical"}
_VIDEO_RENDITIONS = ("large", "medium", "small", "tiny")


def _orientation(width: Any, height: Any) -> Orientation | None:
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return None
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


class PixabayProvider:
    """Searches Pixabay videos and images (`safesearch=true` while `filters.safe`)."""

    id = "pixabay"
    kinds = frozenset({Kind.VIDEO, Kind.IMAGE})
    requires_key = True
    # Pixabay API terms require caching results for 24h. Never lower this TTL.
    SEARCH_TTL_S = 86400
    cache_empty_results = True

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        per_page: int = 20,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self.per_page = max(3, min(200, per_page))

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        """Return assets for `kind`; raise ProviderError on any failure."""
        if kind not in self.kinds:
            return []
        key: str | None = None
        try:
            key = self._api_key or config.get_key(self.id)
            if not key:
                raise ProviderError(
                    self.id, "no API key; set CLIPFORAGE_PIXABAY_KEY or [keys] pixabay"
                )
            params: dict[str, str | int] = {
                "key": key,
                "q": query,
                "per_page": self.per_page,
            }
            if filters.safe:
                params["safesearch"] = "true"
            if kind == Kind.IMAGE and filters.orientation in _ORIENTATION_PARAM:
                params["orientation"] = _ORIENTATION_PARAM[filters.orientation]
            url = VIDEOS_URL if kind == Kind.VIDEO else IMAGES_URL
            client = self._client if self._client is not None else http.get_client()
            response = client.get(url, params=params)
            if not response.is_success:
                body = response.text.strip()[:200]
                raise ProviderError(self.id, f"HTTP {response.status_code}: {body}")
            data = response.json()
            hits = data.get("hits") if isinstance(data, dict) else None
            if not isinstance(hits, list):
                raise ProviderError(self.id, "unexpected response: no 'hits' list")
            assets: list[Asset] = []
            for hit in hits:
                if not isinstance(hit, dict) or hit.get("id") is None:
                    continue
                asset = self._video_asset(hit) if kind == Kind.VIDEO else self._image_asset(hit)
                if asset is None:
                    continue
                if (
                    kind == Kind.VIDEO
                    and filters.orientation is not None
                    and asset.orientation != filters.orientation
                ):
                    continue
                assets.append(asset)
            return assets
        except ProviderError as e:
            raise ProviderError(self.id, _scrub(e.message, key)) from None
        except Exception as e:  # noqa: BLE001 - never let any other exception escape search
            # `from None`: the original exception may embed the request URL (and the key).
            raise ProviderError(self.id, _scrub(f"{type(e).__name__}: {e}", key)) from None

    def _title(self, label: str, hit: dict[str, Any]) -> str:
        """Title-cased tags ("Cat, Kitten, Pet"); ``Pixabay <label> <id>`` without tags."""
        tags = [t.strip() for t in str(hit.get("tags") or "").split(",") if t.strip()]
        if not tags:
            return f"Pixabay {label} {hit['id']}"
        return ", ".join(" ".join(w[:1].upper() + w[1:] for w in t.split()) for t in tags)

    def _attribution(self, label: str, hit: dict[str, Any], page_url: str) -> str:
        user = hit.get("user") or "unknown user"
        return f"{label.capitalize()} by {user} from Pixabay ({page_url})"

    def _video_asset(self, hit: dict[str, Any]) -> Asset | None:
        videos = hit.get("videos") or {}
        chosen: dict[str, Any] | None = None
        for name in _VIDEO_RENDITIONS:
            rendition = videos.get(name)
            if isinstance(rendition, dict) and rendition.get("url"):
                chosen = rendition
                break
        if chosen is None:
            return None
        preview_url: str | None = None
        for name in ("tiny", "medium"):
            rendition = videos.get(name)
            if isinstance(rendition, dict) and rendition.get("thumbnail"):
                preview_url = rendition["thumbnail"]
                break
        media_url: str | None = None
        for name in ("tiny", "small"):
            rendition = videos.get(name)
            if isinstance(rendition, dict) and rendition.get("url"):
                media_url = str(rendition["url"])
                break
        page_url = str(hit.get("pageURL") or f"https://pixabay.com/videos/id-{hit['id']}/")
        duration = hit.get("duration")
        width, height = chosen.get("width"), chosen.get("height")
        return Asset(
            provider=self.id,
            id=str(hit["id"]),
            kind=Kind.VIDEO,
            title=self._title("video", hit),
            source_url=page_url,
            preview_url=preview_url,
            media_url=media_url,
            media_type="video/mp4" if media_url else None,
            license=LICENSE,
            attribution_text=self._attribution("video", hit, page_url),
            commercial_ok=True,
            duration_s=float(duration) if duration is not None else None,
            orientation=_orientation(width, height),
            extra={
                "width": width,
                "height": height,
                "download_url": chosen["url"],
                "pixabay_id": hit["id"],
            },
        )

    def _image_asset(self, hit: dict[str, Any]) -> Asset | None:
        download_url = hit.get("largeImageURL") or hit.get("webformatURL")
        if not download_url:
            return None
        page_url = str(hit.get("pageURL") or f"https://pixabay.com/photos/id-{hit['id']}/")
        width, height = hit.get("imageWidth"), hit.get("imageHeight")
        return Asset(
            provider=self.id,
            id=str(hit["id"]),
            kind=Kind.IMAGE,
            title=self._title("image", hit),
            source_url=page_url,
            preview_url=hit.get("previewURL") or hit.get("webformatURL"),
            license=LICENSE,
            attribution_text=self._attribution("image", hit, page_url),
            commercial_ok=True,
            orientation=_orientation(width, height),
            extra={
                "width": width,
                "height": height,
                "download_url": download_url,
                "pixabay_id": hit["id"],
            },
        )

    def resolve(self, asset: Asset) -> DownloadPlan:
        """Plan a plain HTTP download of the chosen rendition."""
        url = asset.extra.get("download_url")
        if not url:
            raise ProviderError(self.id, f"asset {asset.id} has no download_url")
        if asset.kind == Kind.VIDEO:
            ext = "mp4"
        else:
            suffix = PurePosixPath(urlparse(url).path).suffix.lstrip(".").lower()
            ext = suffix or "jpg"
        return DownloadPlan(asset=asset, method="http", url=url, ext=ext)


def _scrub(message: str, key: str | None) -> str:
    """Remove the API key from an error message (httpx errors can embed the request URL)."""
    return message.replace(key, "***") if key else message
