"""Google Fonts provider: font families via the Google Fonts Developer API.

Key required: set `CLIPFORAGE_GOOGLE_FONTS_KEY` or `[keys] google_fonts = "..."` in config.toml.
The API has no text search, so the full family list is fetched once (sorted by popularity),
memoized on the instance for 24h, and filtered locally. The API returns no license; it is
looked up in `data/google_fonts_licenses.json` (families under google/fonts `apache/`, `ufl/`
and `cc-by-sa/`), and everything else is OFL-1.1.
The key is only ever sent as the `key` query param to www.googleapis.com.
"""

import json
import time
from importlib import resources
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

from clipforage import config, http
from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters
from clipforage.providers.base import ProviderError

WEBFONTS_URL = "https://www.googleapis.com/webfonts/v1/webfonts"
DEFAULT_LICENSE = "OFL-1.1"
LICENSES_FILE = "google_fonts_licenses.json"
CATEGORIES = frozenset({"serif", "sans-serif", "monospace", "display", "handwriting"})
_MEMO_TTL_S = 86400.0

_license_map: dict[str, str] | None = None


def load_license_map() -> dict[str, str]:
    """Return the family-slug -> license map shipped as package data (cached)."""
    global _license_map
    if _license_map is None:
        text = (
            resources.files("clipforage.providers.data")
            .joinpath(LICENSES_FILE)
            .read_text(encoding="utf-8")
        )
        _license_map = {str(k): str(v) for k, v in json.loads(text).items()}
    return _license_map


def _slug(family: str) -> str:
    return family.lower().replace(" ", "")


def _https(url: str) -> str:
    return "https://" + url[len("http://") :] if url.startswith("http://") else url


class GoogleFontsProvider:
    """Searches Google Fonts families (filtered locally; the API has no text search)."""

    id = "google_fonts"
    kinds = frozenset({Kind.FONT})
    requires_key = True
    SEARCH_TTL_S = 86400
    cache_empty_results = True

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        max_results: int = 30,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self.max_results = max(1, max_results)
        self._memo: tuple[float, list[dict[str, Any]]] | None = None

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        """Return font assets matching `query`; raise ProviderError on any failure."""
        if kind not in self.kinds:
            return []
        key: str | None = None
        try:
            key = self._api_key or config.get_key(self.id)
            if not key:
                raise ProviderError(
                    self.id, "no API key; set CLIPFORAGE_GOOGLE_FONTS_KEY or [keys] google_fonts"
                )
            items = self._items(key)
            licenses = load_license_map()
            assets: list[Asset] = []
            for item in items:
                if not self._matches(item, query):
                    continue
                assets.append(self._asset(item, licenses))
                if len(assets) >= self.max_results:
                    break
            return assets
        except ProviderError as e:
            raise ProviderError(self.id, _scrub(e.message, key)) from None
        except Exception as e:  # noqa: BLE001 - never let any other exception escape search
            # `from None`: the original exception may embed the request URL (and the key).
            raise ProviderError(self.id, _scrub(f"{type(e).__name__}: {e}", key)) from None

    def _items(self, key: str) -> list[dict[str, Any]]:
        """Return the full family list, from the in-instance memo if younger than 24h."""
        now = time.monotonic()
        if self._memo is not None and now - self._memo[0] < _MEMO_TTL_S:
            return self._memo[1]
        client = self._client if self._client is not None else http.get_client()
        response = client.get(WEBFONTS_URL, params={"key": key, "sort": "popularity"})
        if response.status_code == 403:
            raise ProviderError(self.id, "HTTP 403: API key invalid, missing or not authorized")
        if not response.is_success:
            body = response.text.strip()[:200]
            raise ProviderError(self.id, f"HTTP {response.status_code}: {body}")
        data = response.json()
        raw = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise ProviderError(self.id, "unexpected response: no 'items' list")
        items = [i for i in raw if isinstance(i, dict) and isinstance(i.get("family"), str)]
        self._memo = (now, items)
        return items

    def _matches(self, item: dict[str, Any], query: str) -> bool:
        q = query.strip().lower()
        if not q:
            return True
        if q in CATEGORIES and str(item.get("category") or "").lower() == q:
            return True
        family = item["family"].lower()
        return all(token in family for token in q.split())

    def _asset(self, item: dict[str, Any], licenses: dict[str, str]) -> Asset:
        family: str = item["family"]
        license_id = licenses.get(_slug(family), DEFAULT_LICENSE)
        raw_files = item.get("files")
        files = (
            {str(k): _https(str(v)) for k, v in raw_files.items() if v}
            if isinstance(raw_files, dict)
            else {}
        )
        return Asset(
            provider=self.id,
            id=_slug(family),
            kind=Kind.FONT,
            title=family,
            source_url=f"https://fonts.google.com/specimen/{family.replace(' ', '+')}",
            preview_url=None,
            license=license_id,
            attribution_text=f"{family} font, {license_id} (Google Fonts)",
            commercial_ok=True,
            extra={
                "category": item.get("category"),
                "variants": item.get("variants"),
                "files": files,
                "version": item.get("version"),
                "subsets": item.get("subsets"),
            },
        )

    def resolve(self, asset: Asset) -> DownloadPlan:
        """Plan a plain HTTP download of the regular style (or the first listed file)."""
        files = asset.extra.get("files")
        if not isinstance(files, dict) or not files:
            raise ProviderError(self.id, f"asset {asset.id} has no font files")
        url = files.get("regular") or next(iter(files.values()))
        url = _https(str(url))
        suffix = PurePosixPath(urlparse(url).path).suffix.lstrip(".").lower()
        return DownloadPlan(asset=asset, method="http", url=url, ext=suffix or "ttf")


def _scrub(message: str, key: str | None) -> str:
    """Remove the API key from an error message (httpx errors can embed the request URL)."""
    return message.replace(key, "***") if key else message
