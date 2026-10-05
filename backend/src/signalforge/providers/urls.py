from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that never change page content.
_TRACKING_PARAMS = {"gclid", "fbclid", "yclid", "msclkid", "igshid", "mc_cid", "mc_eid", "ref_src"}


def canonicalize_url(url: str) -> str:
    """Normalise a URL so that trivially different links to the same page compare equal.

    Lowercases scheme and host, drops ``www.``, default ports, fragments, tracking parameters and
    a trailing slash, and sorts the remaining query parameters.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        )
    )
    return urlunsplit((scheme, host, path, query, ""))


def domain_of(url: str) -> str:
    """Registrable-ish host of a URL without ``www.`` (e.g. ``forum.example.com.tr``)."""
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def in_domain(domain: str, parent: str) -> bool:
    """``domain`` is ``parent`` or one of its subdomains (``forum.a.com`` is in ``a.com``)."""
    return domain == parent or domain.endswith(f".{parent}")
