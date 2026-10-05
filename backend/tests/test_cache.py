from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import CacheMode, get_defaults
from signalforge.db.models import CacheEntry
from signalforge.providers.cache import CacheMiss, CacheStore
from signalforge.providers.fetch import CachedFetcher, FetchedPage, FetchStatus
from signalforge.providers.search import CachedSearch, SearchHit, SearchLocale

TR = SearchLocale(gl="tr", hl="tr", google_domain="google.com.tr")


class Counter:
    def __init__(self, response: dict | None = None) -> None:
        self.calls = 0
        self.response = response or {"v": 1}

    def __call__(self) -> dict:
        self.calls += 1
        return {**self.response, "call": self.calls}


def _store(db: sessionmaker[Session], mode: CacheMode, ttl: int | None = 30) -> CacheStore:
    return CacheStore(db, mode, {"test": ttl, "search": ttl, "page": ttl})


def test_live_mode_serves_cache_on_second_call(db) -> None:
    store, compute = _store(db, CacheMode.LIVE), Counter()

    first = store.get_or_compute("test", {"q": "nakliye"}, compute)
    second = store.get_or_compute("test", {"q": "nakliye"}, compute)

    assert (first.hit, second.hit) == (False, True)
    assert second.response == first.response
    assert compute.calls == 1


def test_key_is_independent_of_dict_order(db) -> None:
    store, compute = _store(db, CacheMode.LIVE), Counter()
    store.get_or_compute("test", {"a": 1, "b": "ş"}, compute)
    assert store.get_or_compute("test", {"b": "ş", "a": 1}, compute).hit


def test_live_mode_refreshes_expired_entries(db) -> None:
    store, compute = _store(db, CacheMode.LIVE, ttl=1), Counter()
    store.get_or_compute("test", {"q": 1}, compute)
    with db.begin() as session:
        session.execute(update(CacheEntry).values(created_at=datetime.now(UTC) - timedelta(days=2)))

    assert not store.get_or_compute("test", {"q": 1}, compute).hit
    assert compute.calls == 2


def test_record_mode_always_recomputes_and_overwrites(db) -> None:
    compute = Counter()
    _store(db, CacheMode.LIVE).get_or_compute("test", {"q": 1}, compute)

    recorded = _store(db, CacheMode.RECORD).get_or_compute("test", {"q": 1}, compute)
    replayed = _store(db, CacheMode.REPLAY).get_or_compute("test", {"q": 1}, compute)

    assert not recorded.hit and recorded.response["call"] == 2
    assert replayed.hit and replayed.response["call"] == 2


def test_replay_mode_ignores_ttl_and_raises_on_miss(db) -> None:
    compute = Counter()
    _store(db, CacheMode.LIVE).get_or_compute("test", {"q": 1}, compute)
    with db.begin() as session:
        session.execute(
            update(CacheEntry).values(created_at=datetime.now(UTC) - timedelta(days=999))
        )

    replay = _store(db, CacheMode.REPLAY)
    assert replay.get_or_compute("test", {"q": 1}, compute).hit
    with pytest.raises(CacheMiss):
        replay.get_or_compute("test", {"q": 2}, compute)
    assert compute.calls == 1


def test_uncacheable_responses_are_returned_but_not_stored(db) -> None:
    store, compute = _store(db, CacheMode.LIVE), Counter()
    for _ in range(2):
        result = store.get_or_compute("test", {"q": 1}, compute, cacheable=lambda r: False)
        assert not result.hit
    assert compute.calls == 2


class FakeSearch:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def search(self, query: str, locale: SearchLocale, n: int) -> list[SearchHit]:
        self.calls += 1
        return [SearchHit(rank=1, url="https://a.com.tr", title=f"{query} sonucu")]


def test_cached_search_record_then_replay(db) -> None:
    provider = FakeSearch()
    CachedSearch(provider, _store(db, CacheMode.RECORD)).search("e-irsaliye zorunluluğu", TR, 10)

    replay = CachedSearch(provider, _store(db, CacheMode.REPLAY))
    result = replay.search("e-irsaliye zorunluluğu", TR, 10)

    assert result.cache_hit and provider.calls == 1
    assert result.hits[0].title == "e-irsaliye zorunluluğu sonucu"
    # Locale and n are part of the key.
    with pytest.raises(CacheMiss):
        replay.search("e-irsaliye zorunluluğu", TR, 5)


class FakeFetcher:
    def __init__(self, status: FetchStatus, http_status: int | None = None) -> None:
        self.status, self.http_status, self.calls = status, http_status, 0

    def fetch(self, url: str) -> FetchedPage:
        self.calls += 1
        return FetchedPage(
            url=url,
            canonical_url=url,
            status=self.status,
            http_status=self.http_status,
            fetched_at=datetime.now(UTC),
            text="metin" if self.status is FetchStatus.OK else None,
        )


@pytest.mark.parametrize(
    ("status", "http_status", "calls"),
    [
        (FetchStatus.OK, 200, 1),
        (FetchStatus.HTTP_ERROR, 404, 1),  # permanent: cached
        (FetchStatus.HTTP_ERROR, 503, 2),  # transient: retried
        (FetchStatus.NETWORK_ERROR, None, 2),
    ],
)
def test_cached_fetcher_keys_by_canonical_url_and_skips_transient(
    db, status: FetchStatus, http_status: int | None, calls: int
) -> None:
    inner = FakeFetcher(status, http_status)
    fetcher = CachedFetcher(inner, _store(db, CacheMode.LIVE))  # type: ignore[arg-type]

    fetcher.fetch("https://www.a.com.tr/sayfa/?utm_source=x")
    second = fetcher.fetch("https://a.com.tr/sayfa")

    assert inner.calls == calls
    assert second.cache_hit is (calls == 1)


def test_defaults_ttls_cover_every_namespace() -> None:
    assert set(get_defaults().cache.ttl_days) == {"search", "page", "llm"}
