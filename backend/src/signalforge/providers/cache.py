"""Record/replay cache (plan §11) shared by the search, fetch and LLM providers.

- ``live``: serve an entry younger than its namespace TTL, otherwise call the provider and store.
- ``record``: always call the provider and overwrite the entry.
- ``replay``: serve any stored entry regardless of age; a miss raises :class:`CacheMiss`.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import CacheMode
from signalforge.db.models import CacheEntry


class CacheMiss(LookupError):
    """Raised in replay mode when a request has never been recorded."""


def cache_key(request: dict[str, Any]) -> str:
    """Stable sha256 of a JSON-serialisable request description."""
    canonical = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Cached:
    response: dict[str, Any]
    hit: bool
    created_at: datetime


class CacheStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        mode: CacheMode,
        ttl_days: dict[str, int | None],
    ) -> None:
        self._session_factory = session_factory
        self.mode = mode
        self._ttl_days = ttl_days

    def get_or_compute(
        self,
        namespace: str,
        request: dict[str, Any],
        compute: Callable[[], dict[str, Any]],
        cacheable: Callable[[dict[str, Any]], bool] = lambda _: True,
    ) -> Cached:
        """Return the cached response for ``request`` or compute and store it.

        Responses rejected by ``cacheable`` (e.g. transient network failures) are returned but not
        stored, so the next call retries them.
        """
        key = cache_key(request)
        if self.mode is not CacheMode.RECORD:
            entry = self._load(namespace, key)
            if entry is not None and (self.mode is CacheMode.REPLAY or self._fresh(entry)):
                return Cached(entry.response, hit=True, created_at=entry.created_at)
            if self.mode is CacheMode.REPLAY:
                raise CacheMiss(f"replay mode: no cached {namespace} entry for {request!r}")

        response = compute()
        now = datetime.now(UTC)
        if cacheable(response):
            self._store(namespace, key, request, response, now)
        return Cached(response, hit=False, created_at=now)

    def peek(self, namespace: str, request: dict[str, Any]) -> dict[str, Any] | None:
        """The stored response for ``request`` in any mode and regardless of age, else ``None``.

        For reading back what an earlier stage already fetched (never calls a provider).
        """
        entry = self._load(namespace, cache_key(request))
        return entry.response if entry is not None else None

    def purge(self, namespace: str | None = None) -> int:
        with self._session_factory.begin() as session:
            stmt = delete(CacheEntry)
            if namespace is not None:
                stmt = stmt.where(CacheEntry.namespace == namespace)
            return session.execute(stmt).rowcount

    def _fresh(self, entry: CacheEntry) -> bool:
        ttl = self._ttl_days.get(entry.namespace)
        return ttl is None or datetime.now(UTC) - entry.created_at < timedelta(days=ttl)

    def _load(self, namespace: str, key: str) -> CacheEntry | None:
        with self._session_factory() as session:
            return session.scalar(
                select(CacheEntry).where(CacheEntry.namespace == namespace, CacheEntry.key == key)
            )

    def _store(
        self,
        namespace: str,
        key: str,
        request: dict[str, Any],
        response: dict[str, Any],
        now: datetime,
    ) -> None:
        values = {"request": request, "response": response, "created_at": now}
        stmt = insert(CacheEntry).values(namespace=namespace, key=key, **values)
        stmt = stmt.on_conflict_do_update(index_elements=["namespace", "key"], set_=values)
        with self._session_factory.begin() as session:
            session.execute(stmt)
