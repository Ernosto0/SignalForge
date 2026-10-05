import httpx
import pytest

from signalforge.config import get_defaults
from signalforge.providers.fetch import Fetcher, FetchStatus

ARTICLE = (
    "<html lang='tr'><head><title>Nakliye takibi</title>"
    "<meta property='article:published_time' content='2025-03-01'></head><body><article><p>"
    + "Sevkiyatları Excel'de takip ediyoruz ve her gün iki saat kaybediyoruz. " * 8
    + "</p></article></body></html>"
)


def _fetcher(routes: dict[str, httpx.Response]) -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    config = get_defaults().fetch.model_copy(update={"per_domain_interval_s": 0})
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Fetcher(config, client=client)


def _html(body: str | bytes, content_type: str = "text/html; charset=utf-8") -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": content_type})


def test_extracts_text_and_metadata() -> None:
    page = _fetcher({"https://a.com.tr/yazi": _html(ARTICLE)}).fetch("https://a.com.tr/yazi")

    assert page.status is FetchStatus.OK
    assert page.title == "Nakliye takibi"
    assert page.published_at == "2025-03-01"
    assert page.html_lang == "tr"
    assert "Excel'de takip ediyoruz" in page.text
    assert not page.transient


def test_respects_robots_txt() -> None:
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /private/")
    fetcher = _fetcher({"https://a.com/robots.txt": robots, "https://a.com/ok": _html(ARTICLE)})

    assert fetcher.fetch("https://a.com/private/x").status is FetchStatus.ROBOTS_DISALLOWED
    assert fetcher.fetch("https://a.com/ok").status is FetchStatus.OK


def test_robots_server_error_means_disallow() -> None:
    fetcher = _fetcher({"https://a.com/robots.txt": httpx.Response(503)})
    assert fetcher.fetch("https://a.com/x").status is FetchStatus.ROBOTS_DISALLOWED


@pytest.mark.parametrize(
    ("response", "status", "transient"),
    [
        (httpx.Response(404), FetchStatus.HTTP_ERROR, False),
        (httpx.Response(503), FetchStatus.HTTP_ERROR, True),
        (_html(b"%PDF-1.4", "application/pdf"), FetchStatus.NOT_HTML, False),
        (_html("<html><body><div id='app'></div></body></html>"), FetchStatus.NO_TEXT, False),
        (_html("x" * 3_100_000), FetchStatus.TOO_LARGE, False),
    ],
)
def test_failure_statuses(response: httpx.Response, status: FetchStatus, transient: bool) -> None:
    page = _fetcher({"https://a.com/x": response}).fetch("https://a.com/x")
    assert page.status is status
    assert page.transient is transient


def test_network_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectTimeout("timed out", request=request)

    config = get_defaults().fetch.model_copy(update={"per_domain_interval_s": 0})
    fetcher = Fetcher(config, client=httpx.Client(transport=httpx.MockTransport(handler)))
    page = fetcher.fetch("https://a.com/x")

    assert page.status is FetchStatus.NETWORK_ERROR
    assert page.transient
