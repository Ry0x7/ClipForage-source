"""LottieFiles provider: free Lottie animations via LottieFiles' public GraphQL endpoint.

No key required. The lottiefiles.com HTML search sits behind a bot wall, so this provider POSTs
``searchPublicAnimations`` to ``https://graphql.lottiefiles.com/``. That endpoint is
UNDOCUMENTED and unauthenticated: it may change or be gated at any time, and then this provider
fails with ``ProviderError`` and the search continues without it. Requests go through
``clipforage.scrape`` (robots.txt checked, throttled per host, one request per search, results
cached 24h); disable with ``[scrape] enabled = false``.

Kinds: ``overlay`` downloads the mp4 render (``videoUrl``), ``template`` the Lottie JSON
(``jsonUrl``). The schema exposes no license field, so every result is labelled
"Lottie Simple License (unverified)" with ``commercial_ok=None``: check the animation page.
"""

from typing import Any

import httpx

from clipforage import scrape
from clipforage.models import Asset, DownloadPlan, Kind, Orientation, SearchFilters
from clipforage.providers.base import ProviderError

GRAPHQL_URL = "https://graphql.lottiefiles.com/"
LICENSE = "Lottie Simple License (unverified)"
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
SEARCH_QUERY = (
    "query($q:String!,$first:Int){ searchPublicAnimations(query:$q, first:$first){ edges{ node{ "
    "id name slug url lottieUrl jsonUrl imageUrl videoUrl gifUrl description "
    "createdBy{ username } metadata{ width height duration } } } } }"
)


def _orientation(metadata: dict[str, Any]) -> Orientation | None:
    width, height = metadata.get("width"), metadata.get("height")
    if not isinstance(width, int | float) or not isinstance(height, int | float):
        return None
    if width <= 0 or height <= 0:
        return None
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"


class LottieFilesProvider:
    """Searches free LottieFiles animations (mp4 overlays and Lottie JSON templates)."""

    id = "lottiefiles"
    kinds = frozenset({Kind.OVERLAY, Kind.TEMPLATE})
    requires_key = False
    scrapes = True
    SEARCH_TTL_S = 86400
    cache_empty_results = True

    def __init__(
        self, client: httpx.Client | None = None, throttle: scrape.HostThrottle | None = None
    ) -> None:
        self._client = client
        self._throttle = throttle

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]:
        """Return assets for ``kind``; raise ProviderError on any failure."""
        if kind not in self.kinds:
            return []
        try:
            if not scrape.enabled():
                raise ProviderError(self.id, scrape.DISABLED_MESSAGE)
            if not query.strip():
                return []
            first = min(filters.limit or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
            response = scrape.request(
                "POST",
                GRAPHQL_URL,
                provider=self.id,
                client=self._client,
                throttle=self._throttle,
                json={"query": SEARCH_QUERY, "variables": {"q": query, "first": first}},
            )
            if not response.is_success:
                body = response.text.strip()[:200]
                raise ProviderError(self.id, f"HTTP {response.status_code}: {body}")
            data = response.json()
            if not isinstance(data, dict):
                raise ProviderError(self.id, "unexpected response: not a JSON object")
            if data.get("errors"):
                first_error = data["errors"][0]
                message = (
                    first_error.get("message") if isinstance(first_error, dict) else first_error
                )
                raise ProviderError(self.id, f"GraphQL error: {message}")
            result = (data.get("data") or {}).get("searchPublicAnimations")
            edges = result.get("edges") if isinstance(result, dict) else None
            if not isinstance(edges, list):
                raise ProviderError(self.id, "unexpected response: no searchPublicAnimations edges")
            assets: list[Asset] = []
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict):
                    asset = self._asset(node, kind)
                    if asset is not None:
                        assets.append(asset)
            return assets
        except ProviderError as e:
            raise ProviderError(self.id, e.message) from None
        except Exception as e:  # noqa: BLE001 - never let any other exception escape search
            raise ProviderError(self.id, f"{type(e).__name__}: {e}") from None

    def _asset(self, node: dict[str, Any], kind: Kind) -> Asset | None:
        if node.get("id") is None:
            return None
        if kind == Kind.OVERLAY:
            media_url, media_type, ext = node.get("videoUrl"), "video/mp4", "mp4"
        else:
            media_url, media_type, ext = node.get("jsonUrl"), "application/json", "json"
        if not isinstance(media_url, str) or not media_url.startswith("https://"):
            return None
        name = str(node.get("name") or f"LottieFiles animation {node['id']}")
        creator = node.get("createdBy")
        username = creator.get("username") if isinstance(creator, dict) else None
        username = str(username).lstrip("/") if username else "unknown creator"
        metadata = node.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        duration = metadata.get("duration")
        slug = node.get("slug") or ""
        source_url = node.get("url") or f"https://lottiefiles.com/animations/{slug}"
        preview = node.get("imageUrl") or node.get("gifUrl") or None
        return Asset(
            provider=self.id,
            id=str(node["id"]),
            kind=kind,
            title=name,
            source_url=str(source_url),
            preview_url=str(preview) if preview else None,
            media_url=media_url,
            media_type=media_type,
            license=LICENSE,
            attribution_text=f'"{name}" by {username} on LottieFiles',
            commercial_ok=None,
            duration_s=float(duration) if isinstance(duration, int | float) else None,
            orientation=_orientation(metadata),
            extra={
                "download_url": media_url,
                "download_ext": ext,
                "lottie_url": node.get("lottieUrl"),
                "json_url": node.get("jsonUrl"),
                "video_url": node.get("videoUrl"),
                "gif_url": node.get("gifUrl"),
                "description": node.get("description"),
            },
        )

    def resolve(self, asset: Asset) -> DownloadPlan:
        """Plan an HTTP download of the mp4 (overlay) or Lottie JSON (template)."""
        url = asset.extra.get("download_url")
        if not isinstance(url, str) or not url:
            raise ProviderError(self.id, f"asset {asset.id} has no download_url")
        ext = str(
            asset.extra.get("download_ext") or ("mp4" if asset.kind == Kind.OVERLAY else "json")
        )
        return DownloadPlan(asset=asset, method="http", url=url, ext=ext)
