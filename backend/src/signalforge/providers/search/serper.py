from typing import Any

import httpx

from signalforge.providers.search.base import SearchError, SearchHit, SearchLocale

SERPER_URL = "https://google.serper.dev/search"


class SerperSearch:
    """Google results via Serper (https://serper.dev).

    Serper has no ``google_domain`` parameter; ``gl``/``hl`` select the country and language.
    Up to 10 results cost one credit, 11–100 cost two, and only successful answers are charged.
    """

    name = "serper"

    def __init__(self, api_key: str, client: httpx.Client | None = None, timeout_s: float = 30):
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout_s)

    def search(self, query: str, locale: SearchLocale, n: int) -> list[SearchHit]:
        payload = {"q": query, "gl": locale.gl, "hl": locale.hl, "num": n}
        try:
            response = self._client.post(
                SERPER_URL, json=payload, headers={"X-API-KEY": self._api_key}
            )
        except httpx.HTTPError as exc:
            raise SearchError(f"Serper request failed: {exc}") from exc

        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            body = {}
        if response.is_error:
            message = body.get("message") or response.text[:200]
            raise SearchError(f"Serper error (HTTP {response.status_code}): {message}")
        if not body:
            raise SearchError("Serper returned a response that is not JSON")

        # An empty result page has no `organic` key at all; that is a valid (empty) answer.
        return [
            SearchHit(
                rank=r.get("position", i + 1),
                url=r["link"],
                title=r.get("title"),
                snippet=r.get("snippet"),
                date=r.get("date"),
            )
            for i, r in enumerate(body.get("organic", []))
            if r.get("link")
        ][:n]
