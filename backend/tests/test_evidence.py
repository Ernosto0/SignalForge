"""Evidence core (plan §7, §8.1): quote verification, independence, evidence strength."""

from datetime import UTC, datetime

import pytest

from signalforge.config import get_defaults
from signalforge.evidence.documents import page_date
from signalforge.evidence.independence import (
    author_hash,
    same_author_groups,
    same_quote_groups,
    source_units,
)
from signalforge.evidence.quotes import verify_quote
from signalforge.evidence.strength import SignalRef, SourceDoc, evidence_strength
from signalforge.text import normalize_with_map

RATIO = get_defaults().extract.fuzzy_min_ratio
STRENGTH = get_defaults().strength
NOW = datetime(2026, 10, 5, tzinfo=UTC)

SOURCE = (
    "Merhaba arkadaşlar,\n\nİSTANBUL’daki depomuzda irsaliyeleri hâlâ elle   giriyoruz.\n"
    "Şoförler “teslim tutanağını” WhatsApp’tan fotoğraf olarak atıyor — sonra biri tek tek "
    "Excel’e işliyor. Bu iş her gün yarım günümüzü alıyor."
)


# --- quotes ---------------------------------------------------------------------------------


def test_normalize_with_map_points_back_into_the_original() -> None:
    text = "  İSTANBUL’da ­ışık  yok "
    norm, starts, ends = normalize_with_map(text)
    assert norm == "istanbul'da işik yok"
    a = norm.index("işik")
    assert text[starts[a] : ends[a + len("işik") - 1]] == "ışık"
    # Composed and decomposed forms normalise alike.
    assert normalize_with_map("çok")[0] == normalize_with_map("çok")[0] == "çok"
    assert normalize_with_map("i̇stanbul")[0] == "istanbul"


@pytest.mark.parametrize(
    "quote",
    [
        "İSTANBUL’daki depomuzda irsaliyeleri hâlâ elle giriyoruz.",  # whitespace collapsed
        "istanbul'daki depomuzda irsaliyeleri hâlâ elle giriyoruz.",  # İ/I/ı/i, ’ vs '
        "ISTANBUL'DAKI DEPOMUZDA IRSALIYELERI HÂLÂ ELLE GIRIYORUZ.",  # dotless capitals
        'Şoförler "teslim tutanağını" WhatsApp’tan fotoğraf olarak atıyor - sonra',  # “” —
    ],
)
def test_exact_match_ignores_case_quotes_and_whitespace(quote: str) -> None:
    match = verify_quote(quote, SOURCE, RATIO)
    assert match is not None and match.verified == "exact"
    assert match.quote == SOURCE[match.char_start : match.char_end]
    assert match.quote.split()[0] in SOURCE  # the stored quote is the source's own text


def test_near_verbatim_quote_is_fuzzy_and_expanded_to_whole_words() -> None:
    quote = "sonra biri tek tek Excele işliyor. Bu iş her gün yarım günümüzü alıyor"  # ’ dropped
    match = verify_quote(quote, SOURCE, RATIO)
    assert match is not None and match.verified == "fuzzy"
    assert 95 <= match.score < 100
    assert match.quote.startswith("sonra") and match.quote.endswith("alıyor")
    assert match.quote == SOURCE[match.char_start : match.char_end]


@pytest.mark.parametrize(
    "quote",
    [
        "Depomuzda irsaliyeleri otomatik olarak sisteme aktarıyoruz.",  # invented
        "irsaliyeleri elle giriyoruz ve Excel'e işliyor",  # stitched from two places
        SOURCE + " Ek cümle.",  # longer than the source
        "   ",
    ],
)
def test_invented_or_stitched_quotes_are_rejected(quote: str) -> None:
    assert verify_quote(quote, SOURCE, RATIO) is None


# --- independence ---------------------------------------------------------------------------


def test_author_hash_is_salted_casefold_stable_and_skips_placeholders() -> None:
    assert author_hash("Lojistikçi İsmail", "s") == author_hash("lojistikçi  ismail", "s")
    assert author_hash("Lojistikçi İsmail", "s") != author_hash("Lojistikçi İsmail", "t")
    assert author_hash("Misafir", "s") is None
    assert author_hash("ANONİM", "s") is None
    assert author_hash(None, "s") is None
    assert len(author_hash("x", "s") or "") == 64


def test_same_author_groups_and_source_units() -> None:
    groups = same_author_groups({1: {"a"}, 2: {"b"}, 3: {"a", "c"}, 4: {"c"}, 5: set()})
    assert [(g.rule, g.document_ids) for g in groups] == [("same_author", [1, 3, 4])]

    units = source_units([1, 2, 3, 4, 5, 6], [[1, 3, 4], [5, 6, 99]])  # 99: not in this run
    assert units == {1: 1, 2: 2, 3: 1, 4: 1, 5: 5, 6: 5}
    assert len(set(units.values())) == 3


# --- strength -------------------------------------------------------------------------------


def _docs(n: int, **kw: object) -> dict[int, SourceDoc]:
    fields = {"source_category": "forum", "quality_tier": "medium", "snippet_only": False,
              "published_at": NOW, **kw}  # fmt: skip
    return {i: SourceDoc(id=i, **fields) for i in range(1, n + 1)}  # type: ignore[arg-type]


def _strength(docs: dict[int, SourceDoc], units: dict[int, int] | None = None, first_hand=True):
    signals = [SignalRef(i, first_hand) for i in docs]
    return evidence_strength(signals, docs, units or {i: i for i in docs}, STRENGTH, NOW)


def test_same_quote_groups_merge_sources_quoting_one_passage() -> None:
    complaint = "Firmam tarafından gönderilen kargo alıcıya teslim edilmedi ve iade edilmedi."
    groups = same_quote_groups(
        {
            1: {complaint},
            2: {complaint.upper().replace(".", "!")},  # same words, other case / punctuation
            3: {"kargo teslim edilmedi"},  # too short to identify one person's report
            4: {"Kargo teslim edilmedi."},
            5: set(),
        },
        min_words=8,
    )
    assert [(g.rule, g.document_ids) for g in groups] == [("same_quote", [1, 2])]
    units = source_units([1, 2, 3, 4, 5], [g.document_ids for g in groups])
    assert len(set(units.values())) == 4


def test_page_dates_on_or_after_the_fetch_day_are_unknown() -> None:
    fetched = datetime(2026, 10, 6, 18, 40, tzinfo=UTC)
    assert page_date("2026-10-06T00:00:00", fetched) is None  # extraction's "today" fallback
    assert page_date("2026-10-07", fetched) is None
    assert page_date("2026-03-17", fetched) == datetime(2026, 3, 17, tzinfo=UTC)
    assert page_date(None, fetched) is None and page_date("dün", fetched) is None


def test_strength_grows_with_independent_sources_and_saturates() -> None:
    scores = [_strength(_docs(n)).score for n in (1, 3, 10, 30)]
    assert scores[0] < scores[1] < scores[2] == scores[3]
    assert _strength(_docs(10)).components["sources"] == 1.0
    assert all(0 <= s <= 10 for s in scores)


def test_duplicates_count_once() -> None:
    docs = _docs(4)
    collapsed = _strength(docs, {1: 1, 2: 1, 3: 1, 4: 4})
    assert collapsed.independent_sources == 2
    assert collapsed.score < _strength(docs).score


def test_strength_components() -> None:
    old = datetime(2020, 1, 1, tzinfo=UTC)
    base = _strength(_docs(3))
    assert base.components["recency"] == 1.0
    assert _strength(_docs(3, published_at=None)).components["recency"] == STRENGTH.unknown_date
    assert _strength(_docs(3, published_at=old)).components["recency"] == 0.0
    snippet = _strength(_docs(3, snippet_only=True)).components["quality"]
    assert snippet == pytest.approx(0.6 * STRENGTH.snippet_only_factor)
    assert _strength(_docs(3), first_hand=False).components["directness"] == 0.0
    mixed = _docs(3) | {4: SourceDoc(4, "jobs", "high", False, NOW)}
    assert _strength(mixed).components["diversity"] > base.components["diversity"]
    total = sum(STRENGTH.weights[k] * v for k, v in base.components.items()) * 10
    assert base.score == pytest.approx(total, abs=0.01)


def test_strength_of_no_signals_is_zero() -> None:
    assert evidence_strength([], {}, {}, STRENGTH, NOW).score == 0.0
