import pytest

from signalforge.packs import load_pack
from signalforge.providers.urls import canonicalize_url, domain_of


def test_canonicalize_url_collapses_trivial_variants() -> None:
    variants = [
        "https://www.Example.com.tr/forum/konu/?utm_source=x&b=2&a=1#yorum",
        "https://example.com.tr:443/forum/konu?a=1&b=2&gclid=abc",
        "HTTPS://EXAMPLE.COM.TR/forum/konu?a=1&b=2",
    ]
    assert {canonicalize_url(v) for v in variants} == {"https://example.com.tr/forum/konu?a=1&b=2"}


def test_canonicalize_url_keeps_meaningful_differences() -> None:
    assert canonicalize_url("http://a.com/x") != canonicalize_url("https://a.com/x")
    assert canonicalize_url("https://a.com/x?id=1") != canonicalize_url("https://a.com/x?id=2")
    assert canonicalize_url("https://a.com:8080/") == "https://a.com:8080/"


def test_domain_of() -> None:
    assert domain_of("https://www.forum.donanimhaber.com/a") == "forum.donanimhaber.com"


def test_tr_pack_loads_with_locale_and_registry() -> None:
    pack = load_pack("tr")

    assert (pack.search.gl, pack.search.hl, pack.search.google_domain) == (
        "tr",
        "tr",
        "google.com.tr",
    )
    assert pack.currency == "TRY"
    assert "logistics" in pack.industries
    assert any("Excel" in p for p in pack.pain_phrases["workaround"])
    # Subdomains inherit the parent's registry entry; unknown domains get the default tier.
    assert pack.source_for("forum.donanimhaber.com").category == "forum"
    assert pack.tier_for("www.gib.gov.tr") == "high"
    assert pack.tier_for("rastgele-blog.com") == pack.default_tier
    assert pack.source_for("notgib.gov.tr") is None


def test_tr_pack_economics_are_dated_and_sourced() -> None:
    pack = load_pack("tr")

    names = [r.name for r in pack.economics]
    assert len(names) == len(set(names))
    assert pack.economic("usd_try").unit == "TRY/USD"
    assert any(n.startswith("wage_net_monthly_") for n in names)
    for ref in pack.economics:
        assert ref.value > 0 and ref.source_url.startswith("https://")
        assert ref.currency == "TRY" or ref.unit == "hour/month"
    with pytest.raises(KeyError):
        pack.economic("no_such_entry")
