"""Freesound provider: sound effects and music via the Freesound APIv2 text search.

Key required: set `CLIPFORAGE_FREESOUND_KEY` or `[keys] freesound = "..."` in config.toml.
The key is only ever sent as the `Authorization: Token <key>` header to freesound.org (never in
the query string). By default only CC0 and CC BY sounds are returned; pass `include_nc=True` to
also allow CC BY-NC (flagged `commercial_ok=False`) unless `filters.commercial_only` is set.
Downloads use the HQ mp3 preview, which needs no auth; original files require OAuth2 (Later).
"""

from typing import Any

import httpx

from clipforage import config, http
from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers.base import ProviderError

# `/apiv2/search/text/` was deprecated in Nov 2025 and redirects here.
SEARCH_URL = "https://freesound.org/apiv2/search/"
FIELDS = "id,name,url,license,username,previews,duration,type,tags,images,filesize"
_COMMERCIAL_LICENSES = ('"Creative Commons 0"', '"Attribution"')
_NC_LICENSE = '"Attribution NonCommercial"'
# Freesound license names (as used in filters and, per the docs, in the `license` field).
_LICENSE_NAMES: dict[str, str] = {
    "creative commons 0": "CC0 1.0",
    "attribution": "CC BY",
    "attribution noncommercial": "CC BY-NC",
    "sampling+": "Sampling+",
}


def _license_label(raw: str) -> str:
    """Map a Freesound license URL (or license name) to a short label; unknown -> raw."""
    value = raw.strip()
    lowered = value.lower()
    if lowered in _LICENSE_NAMES:
        return _LICENSE_NAMES[lowered]
    if "/publicdomain/zero/1.0" in lowered:
        return "CC0 1.0"
    parts = [p for p in lowered.split("/") if p]
    if "licenses" in parts:
        idx = parts.index("licenses")
        code = parts[idx + 1] if idx + 1 < len(parts) else ""
        version = parts[idx + 2] if idx + 2 < len(parts) else ""
        if code == "by":
            return f"CC BY {version}".strip()
        if code == "by-nc":
            return f"CC BY-NC {version}".strip()
        if code == "sampling+":
            return "Sampling+"
    return value or "unknown"


def _commercial_ok(raw: str) -> bool:
    lowered = raw.lower()
    return not any(s in lowered for s in ("by-nc", "noncommercial", "sampling"))


def _num(value: float | None) -> str:
    return "*" if value is None else f"{value:g}"


class FreesoundProvider:
    """Searches Freesound sounds (CC0/CC BY by default; CC BY-NC opt-in)."""

    id = "freesound"
    kinds = frozenset({Kind.AUDIO_SFX, Kind.AUDIO_MUSIC})
    requires_key = True
    SEARCH_TTL_S = 3600

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        page_size: int = 20,
        include_nc: bool = False,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self.page_size = page_size
        self.include_nc = include_nc

    def _filter(self, kind: Kind, filters: SearchFilters) -> str:
        licenses = list(_COMMERCIAL_LICENSES)
        if self.include_nc and not filters.commercial_only:
            licenses.append(_NC_LICENSE)
        clauses = [f"license:({' OR '.join(licenses)})"]
        if filters.min_duration is not None or filters.max_duration is not None:
            clauses.append(
                f"duration:[{_num(filters.min_duration)} TO {_num(filters.max_duration)}]"
            )
        if kind == Kind.AUDIO_MUSIC:
            clauses.append("tag:music")
        return " ".join(clauses)

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        """Return assets for `kind`; raise ProviderError on any failure."""
        if kind not in self.kinds:
            return []
        key: str | None = None
        try:
            key = self._api_key or config.get_key(self.id)
            if not key:
                raise ProviderError(
                    self.id, "no API key; set CLIPFORAGE_FREESOUND_KEY or [keys] freesound"
                )
            params: dict[str, str | int] = {
                "query": query,
                "page_size": self.page_size,
                "sort": "score",
                "fields": FIELDS,
                "filter": self._filter(kind, filters),
            }
            client = self._client if self._client is not None else http.get_client()
            response = client.get(
                SEARCH_URL, params=params, headers={"Authorization": f"Token {key}"}
            )
            if response.status_code == 401:
                raise ProviderError(
                    self.id, "HTTP 401: invalid API key; check CLIPFORAGE_FREESOUND_KEY"
                )
            if not response.is_success:
                body = response.text.strip()[:200]
                raise ProviderError(self.id, f"HTTP {response.status_code}: {body}")
            data = response.json()
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise ProviderError(self.id, "unexpected response: no 'results' list")
            assets: list[Asset] = []
            for result in results:
                if not isinstance(result, dict) or result.get("id") is None:
                    continue
                asset = self._asset(result, kind)
                if asset is not None:
                    assets.append(asset)
            return assets
        except ProviderError as e:
            raise ProviderError(self.id, _scrub(e.message, key)) from None
        except Exception as e:  # noqa: BLE001 - never let any other exception escape search
            raise ProviderError(self.id, _scrub(f"{type(e).__name__}: {e}", key)) from None

    def _asset(self, result: dict[str, Any], kind: Kind) -> Asset | None:
        previews = result.get("previews")
        if not isinstance(previews, dict):
            return None
        hq = previews.get("preview-hq-mp3")
        lq = previews.get("preview-lq-mp3")
        if not hq and not lq:
            return None
        name = str(result.get("name") or f"Freesound sound {result['id']}")
        username = result.get("username") or "unknown user"
        raw_license = str(result.get("license") or "")
        label = _license_label(raw_license) if raw_license else "unknown"
        source_url = str(result.get("url") or f"https://freesound.org/s/{result['id']}/")
        duration = result.get("duration")
        images = result.get("images")
        images = images if isinstance(images, dict) else {}
        thumbnail = images.get("waveform_m") or images.get("spectral_m") or None
        return Asset(
            provider=self.id,
            id=str(result["id"]),
            kind=kind,
            title=name,
            source_url=source_url,
            preview_url=thumbnail,
            media_url=lq or hq,
            media_type="audio/mpeg",
            license=label,
            attribution_text=f'"{name}" by {username} on Freesound ({label})',
            commercial_ok=_commercial_ok(raw_license) if raw_license else None,
            duration_s=float(duration) if isinstance(duration, (int, float)) else None,
            extra={
                "download_url": hq or lq,
                "original_type": result.get("type"),
                "license_url": raw_license,
            },
        )

    def resolve(self, asset: Asset) -> DownloadPlan:
        """Plan an HTTP download of the HQ mp3 preview.

        Previews need no auth. Original files require OAuth2 and are Later.
        """
        url = asset.extra.get("download_url")
        if not url:
            raise ProviderError(self.id, f"asset {asset.id} has no download_url")
        return DownloadPlan(asset=asset, method="http", url=url, ext="mp3")


def _scrub(message: str, key: str | None) -> str:
    """Remove the API key from an error message."""
    return message.replace(key, "***") if key else message
