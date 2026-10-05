"""Competitor research (M4): competitors only from their own pages, verified facts and prices,
a gap matrix whose every cell is a cited fact or ``unknown`` (the M4 exit), and gap inferences."""

import json
import re
from datetime import UTC, datetime

from fakes import OTHER, PACK, PLAN, FakeLLM, make_context
from sqlalchemy import func, select

from signalforge.agents.loop import LoopAction
from signalforge.config import get_defaults
from signalforge.db.models import (
    Claim,
    Competitor,
    Document,
    Excerpt,
    GapMatrix,
    ProblemCluster,
    Query,
    Signal,
)
from signalforge.domain.competitors import (
    CompetitorFact,
    CompetitorPage,
    CompetitorSeeds,
    GapCell,
    GapDimensionDraft,
    GapMatrixDraft,
)
from signalforge.evidence.claims import add_fact, add_inference
from signalforge.evidence.gaps import check_gap_matrices
from signalforge.pipeline.runner import create_run, run_stage
from signalforge.pipeline.stages.competitors import (
    Competitors,
    amount_in_quote,
    numbers_in,
    same_product,
    same_site,
    validate_matrix,
)
from signalforge.providers.search import SearchHit, SearchLocale

DEFAULTS = get_defaults()
CFG = DEFAULTS.competitors

PAGES = {
    "/tiger": "Logo Tiger ile işinizi yönetin. Aylık 1.250,00 TL + KDV. e-İrsaliye entegrasyonu "
    "vardır. Kurulum için uzman desteği gerekir.",
    "/logo-tiger-destek": "Logo Tiger destek ekibine ulaşmak çok zor, haftalardır cevap yok.",
    "/rota": "Rota Pro şoför uygulaması ile teslimat fotoğrafı alınır.",
    "/ghost-review": "Ghost uygulaması irsaliyeleri otomatik okuyor, çok memnunuz.",
}
HITS = [
    ("https://www.logo.com.tr/tiger", "Logo Tiger"),
    ("https://www.sikayetvar.com/logo-tiger-destek", "Logo Tiger şikayet"),
    ("https://rota.com.tr/rota", "Rota Pro"),
    ("https://inceleme.com/ghost-review", "Ghost inceleme"),
]


class CompetitorSearch:
    name = "competitor-fake"

    def search(self, query: str, locale: SearchLocale, n: int) -> list[SearchHit]:
        return [SearchHit(rank=i, url=u, title=t) for i, (u, t) in enumerate(HITS, 1)]


def _fact(kind: str, quote: str, statement: str, **kw) -> CompetitorFact:
    return CompetitorFact(kind=kind, quote=quote, translation="t", statement=statement, **kw)


def _facts(prompt_input: str) -> CompetitorPage:
    url = json.loads(prompt_input.split("# Page\n", 1)[1].split("\n\n# Text", 1)[0])["url"]
    if url.endswith("/tiger"):
        return CompetitorPage(
            competitor_name="Logo Tiger", product_url="https://www.logo.com.tr",
            page_kind="own_site", segment="KOBİ'ler", geo="turkey",
            facts=[
                _fact("price", "Aylık 1.250,00 TL + KDV.", "Costs 1,250 TRY a month.",
                      amount=1250.0, currency="TRY", period="month"),
                _fact("price", "Aylık 1.250,00 TL + KDV.", "Costs 999 TRY.", amount=999.0,
                      currency="TRY", period="month"),  # amount not in the quote
                _fact("integration", "e-İrsaliye entegrasyonu vardır.", "Integrates e-İrsaliye."),
                _fact("feature", "Bu cümle sayfada yok.", "Invented."),  # not on the page
            ],
        )  # fmt: skip
    if url.endswith("/logo-tiger-destek"):
        return CompetitorPage(
            competitor_name="Logo Tiger", product_url=None, page_kind="complaint", segment=None,
            geo="turkey",
            facts=[_fact("review_complaint", "Logo Tiger destek ekibine ulaşmak çok zor",
                         "Support is hard to reach.")],
        )  # fmt: skip
    if url.endswith("/rota"):
        return CompetitorPage(
            competitor_name="Rota Pro", product_url="rota.com.tr", page_kind="own_site",
            segment=None, geo="turkey",
            facts=[_fact("feature", "şoför uygulaması ile teslimat fotoğrafı alınır",
                         "Drivers take delivery photos in the app.")],
        )  # fmt: skip
    return CompetitorPage(
        competitor_name="Ghost", product_url="https://ghost.app", page_kind="review", segment=None,
        geo="unknown",
        facts=[_fact("feature", "Ghost uygulaması irsaliyeleri otomatik okuyor",
                     "Reads delivery notes automatically.")],
    )  # fmt: skip


def _script(prompt_input: str) -> LoopAction:
    history = prompt_input.split("# History\n", 1)[1]
    done = len(re.findall(r"^step \d+:", history, re.MULTILINE))
    script = [
        LoopAction(action="search", query="logo tiger fiyat", reason="r"),
        *(LoopAction(action="fetch", hit_id=i, reason="r") for i in (1, 2, 3, 4)),
    ]
    return script[done] if done < len(script) else LoopAction(action="finish", reason="done")


def _matrix(prompt_input: str) -> GapMatrixDraft:
    payload = json.loads(prompt_input)
    claims = {
        (p["product"], c["kind"]): c["claim"] for p in payload["products"] for c in p["claims"]
    }
    logo = next(p["product"] for p in payload["products"] if p["name"] == "Logo Tiger")
    rota = next(p["product"] for p in payload["products"] if p["name"] == "Rota Pro")
    cell = GapCell
    return GapMatrixDraft(
        dimensions=[
            GapDimensionDraft(key="driver_app", label="Driver app", from_signals=[1]),
            GapDimensionDraft(key="made_up", label="Not from evidence", from_signals=[99]),
            GapDimensionDraft(key="price_for_smb", label="x", from_signals=[]),
        ],
        cells=[
            cell(dimension="price_for_smb", competitor=logo, value="partial",
                 claim=claims[(logo, "price")]),
            cell(dimension="e_document_integration", competitor=logo, value="yes",
                 claim=claims[(logo, "integration")]),
            cell(dimension="setup_effort", competitor=logo, value="no",
                 claim=claims[(logo, "review_complaint")]),
            cell(dimension="driver_app", competitor=logo, value="yes", claim=999),  # bogus
            # Rota cites Logo's claim: demoted.
            cell(dimension="e_document_integration", competitor=rota, value="yes",
                 claim=claims[(logo, "integration")]),
            cell(dimension="driver_app", competitor=rota, value="yes",
                 claim=claims[(rota, "feature")]),
        ],
    )  # fmt: skip


def _seed(db, run_id: int) -> int:
    with db.begin() as session:
        doc = Document(run_id=run_id, url="https://f.com/t", canonical_url="https://f.com/t",
                       domain="f.com", source_category="forum", quality_tier="medium",
                       published_at=datetime.now(UTC))  # fmt: skip
        session.add(doc)
        session.flush()
        fact_ids, signal_ids = [], []
        for i, (kind, quote) in enumerate(
            [("tool_complaint", "Logo Tiger irsaliye ekranı çok yavaş."),
             ("complaint", "Şoförlerden teslimat fotoğrafını WhatsApp ile topluyoruz.")]
        ):  # fmt: skip
            excerpt = Excerpt(run_id=run_id, document_id=doc.id, quote=quote, translation="t",
                              verified="exact", char_start=i, char_end=i + 1)  # fmt: skip
            session.add(excerpt)
            session.flush()
            signal = Signal(run_id=run_id, excerpt_id=excerpt.id, type=kind, actor="nakliyeci",
                            workflow="delivery", statement=f"Signal {i}.",
                            first_hand=True)  # fmt: skip
            session.add(signal)
            session.flush()
            fact_ids.append(add_fact(session, run_id, f"Signal {i}.", [excerpt.id],
                                     stage="extract").id)  # fmt: skip
            signal_ids.append(signal.id)
        inference = add_inference(session, run_id, "Carriers struggle.", fact_ids, stage="cluster")
        cluster = ProblemCluster(run_id=run_id, name="Proof of delivery", description="d",
                                 signal_ids=signal_ids, shortlisted=True, claim_id=inference.id,
                                 evidence_strength=6.0)  # fmt: skip
        session.add(cluster)
        session.flush()
        return cluster.id


def _page(path: str) -> str:
    # Padding: the fetcher treats pages under `fetch.min_text_chars` as empty.
    return f"{PAGES.get(path, '')} {OTHER * 2}"


def _context(db, run_id: int, llm: FakeLLM):
    return make_context(db, run_id, CompetitorSearch(), llm, page_text=_page)


def _llm() -> FakeLLM:
    return FakeLLM(
        {
            CompetitorSeeds: lambda _: CompetitorSeeds(
                from_signals=["Logo Tiger"], suggested=["Acme ERP"]
            ),
            LoopAction: _script,
            CompetitorPage: _facts,
            GapMatrixDraft: _matrix,
        }
    )


def test_competitors_stage_builds_a_cited_gap_matrix(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    problem_id = _seed(db, run_id)
    llm = _llm()
    ctx = _context(db, run_id, llm)

    row = run_stage(ctx, Competitors())
    assert row.status == "completed", row.error
    m = row.metrics
    assert (m["competitors"], m["pages_read"], m["prices"]) == (2, 4, 1)
    assert (m["facts_verified"], m["facts_unverified"]) == (5, 1)
    assert m["prices_without_amount_in_quote"] == 1

    with db() as session:
        competitors = {c.name: c for c in session.scalars(select(Competitor))}
        facts = session.scalars(select(Claim).where(Claim.stage == "competitors",
                                                    Claim.kind == "fact")).all()  # fmt: skip
        (matrix,) = session.scalars(select(GapMatrix)).all()
        gaps = session.scalars(select(Claim).where(Claim.kind == "inference",
                                                   Claim.stage == "competitors")).all()  # fmt: skip
        docs = session.scalars(select(Document).where(Document.origin == "competitor")).all()
        queries = session.scalars(select(Query).where(Query.intent == "competitor")).all()
        errors = check_gap_matrices(session, run_id)

    # Ghost was only seen on a review site, Acme only suggested by the model: neither is stored.
    assert sorted(competitors) == ["Logo Tiger", "Rota Pro"]
    logo, rota = competitors["Logo Tiger"], competitors["Rota Pro"]
    assert (logo.url, logo.segment, logo.geo) == ("https://www.logo.com.tr", "KOBİ'ler", "turkey")
    (price,) = logo.pricing
    assert (price["amount"], price["currency"], price["period"]) == (1250.0, "TRY", "month")
    assert price["observed_at"] and price["claim_id"] in {f.id for f in facts}
    # The complaint page attached to Logo Tiger by name.
    logo_facts = [f for f in facts if f.meta["competitor_id"] == logo.id]
    assert sorted(f.meta["kind"] for f in logo_facts) == [
        "integration",
        "price",
        "review_complaint",
    ]
    assert len(docs) == 3 and all(d.problem_id == problem_id for d in docs)
    assert len(queries) == 1 and queries[0].meta["problem_ids"] == [problem_id]

    # The M4 exit: every cell is a cited fact or unknown.
    assert errors == []
    mx = matrix.matrix
    keys = [d["key"] for d in mx["dimensions"]]
    assert keys[:2] == ["driver_app", "price_for_smb"] and "made_up" not in keys
    assert set(CFG.always_dimensions) <= set(keys)
    cell = lambda dim, c: mx["cells"][dim][str(c.id)]  # noqa: E731
    assert cell("e_document_integration", logo)["value"] == "yes"
    assert cell("e_document_integration", rota) == {"value": "unknown", "claim_id": None}
    assert cell("driver_app", logo) == {"value": "unknown", "claim_id": None}
    assert cell("driver_app", rota)["value"] == "yes"
    assert cell("turkish_localization", logo)["value"] == "unknown"  # never answered
    assert row.metrics["demoted_cells"] == 2
    # Gaps: price (partial) and setup (no) have evidence and no "yes"; all-unknown columns don't.
    assert sorted(g["dimension"] for g in mx["gaps"]) == ["price_for_smb", "setup_effort"]
    assert len(gaps) == 2 and all(g.derived_from for g in gaps)

    # Re-running replaces the stage's outputs; answers come from the LLM cache.
    with db() as session:
        before = {
            t.__name__: session.scalar(select(func.count()).select_from(t))
            for t in (Competitor, GapMatrix, Claim, Document, Excerpt, Query)
        }
    calls = len(llm.calls)
    assert run_stage(ctx, Competitors()).status == "completed"
    with db() as session:
        after = {
            t.__name__: session.scalar(select(func.count()).select_from(t))
            for t in (Competitor, GapMatrix, Claim, Document, Excerpt, Query)
        }
        assert check_gap_matrices(session, run_id) == []
    assert after == before and len(llm.calls) == calls


def test_exit_check_catches_a_bad_cell(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    _seed(db, run_id)
    ctx = _context(db, run_id, _llm())
    run_stage(ctx, Competitors())
    with db.begin() as session:
        matrix = session.scalars(select(GapMatrix)).one()
        competitors = {c.name: c.id for c in session.scalars(select(Competitor))}
        logo_claim = matrix.matrix["cells"]["e_document_integration"][
            str(competitors["Logo Tiger"])
        ]["claim_id"]
        cells = json.loads(json.dumps(matrix.matrix["cells"]))
        cells["e_document_integration"][str(competitors["Rota Pro"])] = {
            "value": "yes", "claim_id": logo_claim,
        }  # fmt: skip
        cells["setup_effort"][str(competitors["Rota Pro"])] = {"value": "no", "claim_id": None}
        del cells["driver_app"][str(competitors["Logo Tiger"])]
        matrix.matrix = {**matrix.matrix, "cells": cells}
    with db() as session:
        errors = check_gap_matrices(session, run_id)
    assert len(errors) == 3
    assert any("another competitor" in e for e in errors)
    assert any("without a citation" in e for e in errors)
    assert any("no cell" in e for e in errors)


def test_failed_entailment_demotes_a_cell() -> None:
    draft = GapMatrixDraft(
        dimensions=[],
        cells=[GapCell(dimension="setup_effort", competitor=1, value="no", claim=1)],
    )
    m = validate_matrix(draft, 1, {}, {1: (10, 1)}, {1: False}, CFG)
    assert m.cells["setup_effort"][1] == {"value": "unknown", "claim_no": None}
    assert m.notes["demoted_failed_entailment"] == 1
    ok = validate_matrix(draft, 1, {}, {1: (10, 1)}, {1: True}, CFG)
    assert ok.cells["setup_effort"][1] == {"value": "no", "claim_no": 1}


def test_price_amounts_must_be_in_the_quote() -> None:
    assert numbers_in("Aylık 1.250,50 TL") >= {1250.5}
    assert numbers_in("$1,250.50 per month") >= {1250.5}
    assert amount_in_quote(1250, "Aylık 1.250 TL + KDV")
    assert amount_in_quote(49.9, "kullanıcı başı 49,90 TL")
    assert not amount_in_quote(999, "Aylık 1.250 TL")


def test_names_and_sites() -> None:
    assert same_product("Logo Tiger", "LOGO TİGER") and same_product("Logo", "Logo Tiger 3")
    assert not same_product("Logo", "Logolu Takip")
    assert same_site("https://destek.logo.com.tr/x", "https://www.logo.com.tr")
    assert same_site("https://logo.com.tr/x", "logo.com.tr")
    assert not same_site("https://www.sikayetvar.com/logo", "https://www.logo.com.tr")
    assert not same_site("https://logo.com.tr", None)
