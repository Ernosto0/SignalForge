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
from signalforge.providers.search.serper import SerperSearch

__all__ = [
    "CachedSearch",
    "SearchError",
    "SearchHit",
    "SearchLocale",
    "SearchProvider",
    "SearchResponse",
    "make_search_provider",
    "search_api_key",
    "search_with_retries",
]


class _MissingKeySearch:
    """Stand-in used when no key is configured, so replay mode still works without one."""

    def __init__(self, name: str, env_var: str) -> None:
        self.name = name
        self._env_var = env_var

    def search(self, query: str, locale: SearchLocale, n: int) -> list[SearchHit]:
        raise SearchError(f"{self.name}: no API key configured (set {self._env_var} in .env)")


# SEARCH_PROVIDER -> (provider class, Settings field holding its key)
_PROVIDERS = {
    "serpapi": (SerpApiSearch, "serp_api_key"),
    "serper": (SerperSearch, "serper_api_key"),
}


def search_api_key(settings: Settings) -> str | None:
    """The configured key of the selected provider, or None (also for an unknown provider)."""
    if settings.search_provider not in _PROVIDERS:
        return None
    key = getattr(settings, _PROVIDERS[settings.search_provider][1])
    return key.get_secret_value() if key is not None and key.get_secret_value() else None


def make_search_provider(settings: Settings, timeout_s: float = 30) -> SearchProvider:
    if settings.search_provider not in _PROVIDERS:
        raise ValueError(f"unknown SEARCH_PROVIDER {settings.search_provider!r}")
    provider_cls, field = _PROVIDERS[settings.search_provider]
    key = search_api_key(settings)
    if key is None:
        return _MissingKeySearch(settings.search_provider, field.upper())
    return provider_cls(key, timeout_s=timeout_s)
