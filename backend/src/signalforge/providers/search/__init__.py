from signalforge.config import Settings
from signalforge.providers.search.base import (
    CachedSearch,
    SearchError,
    SearchHit,
    SearchLocale,
    SearchProvider,
    SearchResponse,
    search_with_retries,
)
from signalforge.providers.search.serpapi import SerpApiSearch

__all__ = [
    "CachedSearch",
    "SearchError",
    "SearchHit",
    "SearchLocale",
    "SearchProvider",
    "SearchResponse",
    "make_search_provider",
    "search_with_retries",
]


class _MissingKeySearch:
    """Stand-in used when no key is configured, so replay mode still works without one."""

    def __init__(self, name: str) -> None:
        self.name = name

    def search(self, query: str, locale: SearchLocale, n: int) -> list[SearchHit]:
        raise SearchError(f"{self.name}: no API key configured (set SERP_API_KEY in .env)")


def make_search_provider(settings: Settings, timeout_s: float = 30) -> SearchProvider:
    if settings.search_provider != "serpapi":
        raise ValueError(f"unknown SEARCH_PROVIDER {settings.search_provider!r}")
    if settings.serp_api_key is None or not settings.serp_api_key.get_secret_value():
        return _MissingKeySearch("serpapi")
    return SerpApiSearch(settings.serp_api_key.get_secret_value(), timeout_s=timeout_s)
