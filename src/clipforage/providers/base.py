"""Provider Protocol and the error type providers raise on failure."""

from typing import Protocol, runtime_checkable

from clipforage.models import Asset, DownloadPlan, Kind, SearchFilters


class ProviderError(Exception):
    """A failure attributable to a single provider."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(provider, message)
        self.provider = provider
        self.message = message

    def __str__(self) -> str:
        return f"{self.provider}: {self.message}"


@runtime_checkable
class Provider(Protocol):
    """A searchable source of assets.

    Optional attributes, read with ``getattr`` (not part of the structural check):
    ``SEARCH_TTL_S`` (search cache TTL in seconds), ``scrapes`` (HTML scraping, gated by
    ``scrape.enabled()``) and ``cache_empty_results`` (default False: an empty result is not
    cached; True for stable sources whose [] means genuinely no results).
    """

    id: str
    kinds: frozenset[Kind]
    requires_key: bool

    def search(self, query: str, kind: Kind, filters: SearchFilters) -> list[Asset]: ...

    def resolve(self, asset: Asset) -> DownloadPlan: ...
