import httpx
import pytest

from signalforge.providers.search import SearchError, SearchLocale
from signalforge.providers.search.serpapi import SerpApiSearch

TR = SearchLocale(gl="tr", hl="tr", google_domain="google.com.tr")


def _provider(handler) -> SerpApiSearch:
    return SerpApiSearch("key", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_parses_organic_results_and_sends_locale() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "position": 1,
                        "link": "https://a.com.tr/x",
                        "title": "Nakliye",
                        "snippet": "s",
                    },
                    {"position": 2, "title": "no link — skipped"},
                    {"position": 3, "link": "https://b.com/y", "date": "3 gün önce"},
                ]
            },
        )

    hits = _provider(handler).search("nakliye Excel'de takip", TR, n=10)

    assert [h.rank for h in hits] == [1, 3]
    assert hits[0].title == "Nakliye" and hits[1].date == "3 gün önce"
    assert seen["q"] == "nakliye Excel'de takip"
    assert (seen["gl"], seen["hl"], seen["google_domain"]) == ("tr", "tr", "google.com.tr")


def test_truncates_to_n() -> None:
    results = [{"position": i, "link": f"https://a.com/{i}"} for i in range(1, 11)]
    provider = _provider(lambda _: httpx.Response(200, json={"organic_results": results}))
    assert len(provider.search("q", TR, n=3)) == 3


def test_empty_result_page_is_not_an_error() -> None:
    body = {"error": "Google hasn't returned any results for this query."}
    assert _provider(lambda _: httpx.Response(200, json=body)).search("q", TR, 10) == []


def test_api_error_raises() -> None:
    provider = _provider(lambda _: httpx.Response(401, json={"error": "Invalid API key."}))
    with pytest.raises(SearchError, match="Invalid API key"):
        provider.search("q", TR, 10)
