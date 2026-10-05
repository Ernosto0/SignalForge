"""Generic page fetcher: httpx + trafilatura, robots.txt, per-domain rate limit (plan §10).

No JS rendering in V0: pages whose extracted text is too short are reported as ``no_text``.
"""

import re
import threading
import time
from datetime import UTC, datetime
from enum import StrEnum
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from pydantic import BaseModel

from signalforge.config import FetchDefaults
from signalforge.providers.cache import CacheStore
from signalforge.providers.urls import canonicalize_url, domain_of

_HTML_TYPES = ("text/html", "application/xhtml+xml")
_HTML_LANG = re.compile(rb"<html[^>]*\slang=[\"']?([a-zA-Z]{2,3})", re.IGNORECASE)


class FetchStatus(StrEnum):
    OK = "ok"
    ROBOTS_DISALLOWED = "robots_disallowed"
    HTTP_ERROR = "http_error"
    NOT_HTML = "not_html"
    TOO_LARGE = "too_large"
    NO_TEXT = "no_text"  # empty or JS-rendered page
    NETWORK_ERROR = "network_error"


class FetchedPage(BaseModel):
    url: str
    canonical_url: str
    status: FetchStatus
    fetched_at: datetime
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    title: str | None = None
    text: str | None = None
    published_at: str | None = None  # ISO date as extracted, if any
    author: str | None = None  # raw; hash before storing as evidence (KVKK)
    html_lang: str | None = None
    error: str | None = None
    cache_hit: bool = False

    @property
    def transient(self) -> bool:
        """Failures worth retrying later; never cached."""
        return self.status is FetchStatus.NETWORK_ERROR or (
            self.status is FetchStatus.HTTP_ERROR
            and (self.http_status is None or self.http_status == 429 or self.http_status >= 500)
        )


class _DomainRateLimiter:
    def __init__(self, interval_s: float) -> None:
        self._interval = interval_s
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, domain: str) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(domain, now))
            self._next_slot[domain] = slot + self._interval
        if (delay := slot - now) > 0:
            time.sleep(delay)


class Fetcher:
    """Fetches pages live. Use :class:`CachedFetcher` in pipeline code."""

    def __init__(self, config: FetchDefaults, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(
            timeout=config.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": config.user_agent, "Accept-Language": "tr,en;q=0.5"},
        )
        self._limiter = _DomainRateLimiter(config.per_domain_interval_s)
        self._robots: dict[str, RobotFileParser] = {}
        self._robots_lock = threading.Lock()

    def fetch(self, url: str) -> FetchedPage:
        page = FetchedPage(
            url=url,
            canonical_url=canonicalize_url(url),
            status=FetchStatus.OK,
            fetched_at=datetime.now(UTC),
        )
        try:
            if not self._allowed(url):
                return page.model_copy(update={"status": FetchStatus.ROBOTS_DISALLOWED})
            self._limiter.wait(domain_of(url))
            return self._download(url, page)
        except httpx.HTTPError as exc:
            return page.model_copy(
                update={
                    "status": FetchStatus.NETWORK_ERROR,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    def _download(self, url: str, page: FetchedPage) -> FetchedPage:
        with self._client.stream("GET", url) as response:
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            meta = {
                "final_url": str(response.url),
                "http_status": response.status_code,
                "content_type": content_type or None,
            }
            if response.is_error:
                return page.model_copy(update={**meta, "status": FetchStatus.HTTP_ERROR})
            if content_type and content_type not in _HTML_TYPES:
                return page.model_copy(update={**meta, "status": FetchStatus.NOT_HTML})
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > self._config.max_bytes:
                    return page.model_copy(update={**meta, "status": FetchStatus.TOO_LARGE})

        return page.model_copy(update={**meta, **self._extract(bytes(body), str(response.url))})

    def _extract(self, html: bytes, url: str) -> dict[str, object]:
        lang = _HTML_LANG.search(html[:5000])
        fields: dict[str, object] = {"html_lang": lang.group(1).decode().lower() if lang else None}
        doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=True)
        text = (doc.text if doc else None) or ""
        if len(text) < self._config.min_text_chars:
            return {**fields, "status": FetchStatus.NO_TEXT, "text": text or None}
        return {
            **fields,
            "status": FetchStatus.OK,
            "text": text,
            "title": doc.title,
            "published_at": doc.date,
            "author": doc.author,
        }

    def _allowed(self, url: str) -> bool:
        parts = httpx.URL(url)
        origin = f"{parts.scheme}://{parts.netloc.decode()}"
        with self._robots_lock:
            parser = self._robots.get(origin)
        if parser is None:
            parser = self._load_robots(origin)
            with self._robots_lock:
                self._robots[origin] = parser
        return parser.can_fetch(self._config.user_agent, url)

    def _load_robots(self, origin: str) -> RobotFileParser:
        # RFC 9309: 4xx -> no restrictions; 5xx / unreachable -> assume full disallow.
        parser = RobotFileParser()
        try:
            response = self._client.get(f"{origin}/robots.txt")
        except httpx.HTTPError:
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser
        if response.status_code >= 500:
            parser.parse(["User-agent: *", "Disallow: /"])
        elif response.status_code >= 400:
            parser.parse([])
        else:
            parser.parse(response.text.splitlines())
        return parser


class CachedFetcher:
    """Page cache keyed by canonical URL. Transient failures are not cached."""

    def __init__(self, fetcher: Fetcher, cache: CacheStore) -> None:
        self.fetcher = fetcher
        self.cache = cache

    def fetch(self, url: str) -> FetchedPage:
        canonical = canonicalize_url(url)
        cached = self.cache.get_or_compute(
            "page",
            {"canonical_url": canonical},
            lambda: self.fetcher.fetch(url).model_dump(mode="json", exclude={"cache_hit"}),
            cacheable=lambda r: not FetchedPage.model_validate(r).transient,
        )
        return FetchedPage.model_validate({**cached.response, "cache_hit": cached.hit})

    def cached(self, url: str) -> FetchedPage | None:
        """The stored page for ``url`` without fetching (any cache mode, any age)."""
        response = self.cache.peek("page", {"canonical_url": canonicalize_url(url)})
        return FetchedPage.model_validate({**response, "cache_hit": True}) if response else None
