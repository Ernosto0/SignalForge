import json

import httpx
import pytest

from signalforge.config import Settings
from signalforge.providers.search import SearchError, SearchLocale, make_search_provider
from signalforge.providers.search.serper import SerperSearch

TR = SearchLocale(gl="tr", hl="tr", google_domain="google.com.tr")


def _provider(handler) -> SerperSearch:
    return SerperSearch("key", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_parses_organic_results_and_sends_locale() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["key"] = request.headers["X-API-KEY"]
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "organic": [
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
    assert (seen["method"], seen["key"]) == ("POST", "key")
    assert seen["q"] == "nakliye Excel'de takip"
    assert (seen["gl"], seen["hl"], seen["num"]) == ("tr", "tr", 10)


def test_truncates_to_n() -> None:
    results = [{"position": i, "link": f"https://a.com/{i}"} for i in range(1, 11)]
    provider = _provider(lambda _: httpx.Response(200, json={"organic": results}))
    assert len(provider.search("q", TR, n=3)) == 3


def test_empty_result_page_is_not_an_error() -> None:
    body = {"searchParameters": {"q": "q"}, "credits": 1}
    assert _provider(lambda _: httpx.Response(200, json=body)).search("q", TR, 10) == []


def test_api_error_raises() -> None:
    body = {"message": "Unauthorized.", "statusCode": 403}
    provider = _provider(lambda _: httpx.Response(403, json=body))
    with pytest.raises(SearchError, match=r"HTTP 403.*Unauthorized"):
        provider.search("q", TR, 10)


def test_non_json_error_raises() -> None:
    provider = _provider(lambda _: httpx.Response(502, text="Bad Gateway"))
    with pytest.raises(SearchError, match="Bad Gateway"):
        provider.search("q", TR, 10)


def test_timeout_raises_search_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(SearchError, match="timed out"):
        _provider(handler).search("q", TR, 10)


def test_serper_is_the_default_and_serpapi_stays_selectable() -> None:
    from signalforge.providers.search.serpapi import SerpApiSearch

    default = Settings(_env_file=None, serper_api_key="k")
    assert default.search_provider == "serper"
    assert isinstance(make_search_provider(default), SerperSearch)

    serpapi = Settings(_env_file=None, search_provider="serpapi", serp_api_key="k")
    assert isinstance(make_search_provider(serpapi), SerpApiSearch)


def test_factory_selects_serper_and_its_key() -> None:
    settings = Settings(_env_file=None, search_provider="serper", serper_api_key="k")
    assert isinstance(make_search_provider(settings), SerperSearch)

    missing = make_search_provider(
        Settings(_env_file=None, search_provider="serper", serp_api_key="serpapi-only")
    )
    with pytest.raises(SearchError, match="SERPER_API_KEY"):
        missing.search("q", TR, 10)
