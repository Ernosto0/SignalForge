from typing import Any

import httpx

from signalforge.providers.search.base import SearchError, SearchHit, SearchLocale

SERPAPI_URL = "https://serpapi.com/search.json"

# SerpApi reports an empty result page as an error; that is a valid (empty) answer for us.
_NO_RESULTS = "hasn't returned any results"


class SerpApiSearch:
    """Google results via SerpApi (https://serpapi.com/search-api)."""

    name = "serpapi"

    def __init__(self, api_key: str, client: httpx.Client | None = None, timeout_s: float = 30):
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout_s)

    def search(self, query: str, locale: SearchLocale, n: int) -> list[SearchHit]:
        params = {
            "engine": "google",
            "q": query,
            "gl": locale.gl,
            "hl": locale.hl,
            "google_domain": locale.google_domain,
            "api_key": self._api_key,
        }
        try:
            response = self._client.get(SERPAPI_URL, params=params)
            body: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(f"SerpApi request failed: {exc}") from exc

        if error := body.get("error"):
            if _NO_RESULTS in error:
                return []
            raise SearchError(f"SerpApi error (HTTP {response.status_code}): {error}")
        if response.is_error:
            raise SearchError(f"SerpApi returned HTTP {response.status_code}")

        return [
            SearchHit(
                rank=r.get("position", i + 1),
                url=r["link"],
                title=r.get("title"),
                snippet=r.get("snippet"),
                date=r.get("date"),
            )
            for i, r in enumerate(body.get("organic_results", []))
            if r.get("link")
        ][:n]
