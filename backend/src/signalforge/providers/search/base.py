from typing import Protocol

from pydantic import BaseModel

from signalforge.providers.cache import CacheStore


class SearchLocale(BaseModel):
    """Search-engine locale, taken from the market pack (e.g. gl=tr, hl=tr, google.com.tr)."""

    gl: str
    hl: str
    google_domain: str = "google.com"


class SearchHit(BaseModel):
    rank: int
    url: str
    title: str | None = None
    snippet: str | None = None
    date: str | None = None  # as displayed by the engine; parsed later, if at all


class SearchError(RuntimeError):
    pass


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, locale: SearchLocale, n: int) -> list[SearchHit]: ...


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    cache_hit: bool


class CachedSearch:
    """Wraps a provider with the record/replay cache, keyed by (provider, query, locale, n)."""

    def __init__(self, provider: SearchProvider, cache: CacheStore) -> None:
        self.provider = provider
        self.cache = cache

    def search(self, query: str, locale: SearchLocale, n: int) -> SearchResponse:
        request = {
            "provider": self.provider.name,
            "query": query,
            "locale": locale.model_dump(),
            "n": n,
        }
        cached = self.cache.get_or_compute(
            "search",
            request,
            lambda: {
                "hits": [h.model_dump() for h in self.provider.search(query, locale, n)],
            },
        )
        hits = [SearchHit.model_validate(h) for h in cached.response["hits"]]
        return SearchResponse(hits=hits, cache_hit=cached.hit)
