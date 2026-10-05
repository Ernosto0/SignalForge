"""competitors stage (plan §4; agent-modules.md §5): existing solutions and the gap matrix.

Per shortlisted problem (after verify):

1. **Seeds.** The fast model lists products named in the problem's evidence quotes, plus a few it
   knows of. Names are search seeds only.
2. **Loop.** A bounded search/fetch loop (agents/loop.py) reads each product's own pages (home,
   pricing, features) and review / complaint pages.
3. **Page facts.** Each page read goes through ``competitor_facts``. Every fact's quote is verified
   against the page text (evidence/quotes.py); a price also needs its amount inside the quote.
4. **Competitors.** A product becomes a competitor only through a page on its **own** website
   (the page's domain matches the ``product_url`` it names). Review and complaint pages attach by
   Turkish-aware name match; facts about products never confirmed on their own site are dropped.
   Facts are stored as fact claims with ``meta.competitor_id``, prices also in
   ``Competitor.pricing`` with the date they were observed (never converted here).
5. **Gap matrix.** The analysis model derives dimensions from the problem's signals (plus the
   configured ``always_dimensions``) and fills one cell per dimension × competitor, citing claims
   by number. Every cell that does not cite a fact of *that* competitor which passed entailment is
   demoted to ``unknown`` (evidence/gaps.py re-checks stored matrices: the M4 exit).
6. **Gaps.** A dimension where no competitor is ``yes`` and at least one cell is known becomes an
   inference claim, derived from those cells' claims.
"""

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from signalforge.agents.loop import LoopPage, LoopTrace, run_loop, store_searches
from signalforge.config import CompetitorsDefaults
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
    GapMatrixDraft,
)
from signalforge.domain.plan import ResearchPlan
from signalforge.evidence.claims import add_fact, add_inference, delete_stage_claims
from signalforge.evidence.clusters import cluster_signal_ids, verification
from signalforge.evidence.documents import (
    loop_page_document,
    loop_snippet_document,
    store_document,
)
from signalforge.evidence.entailment import clear_entailment, entail_pending, failed
from signalforge.evidence.extraction import write_excerpt
from signalforge.evidence.gaps import UNKNOWN
from signalforge.evidence.quotes import QuoteMatch, verify_quote
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult, load_run_plan
from signalforge.prompts import load_prompt
from signalforge.providers.cache import cache_key
from signalforge.providers.llm import BudgetExceeded, LLMError, ModelTier
from signalforge.providers.urls import domain_of, in_domain
from signalforge.text import match_tokens

STAGE = "competitors"
_NUMBER = re.compile(r"\d[\d.,]*\d|\d")


@dataclass(frozen=True)
class SignalView:
    id: int
    type: str
    statement: str
    quote: str
    translation: str | None


@dataclass(frozen=True)
class Target:
    """A shortlisted problem as competitor research sees it."""

    id: int
    name: str
    description: str
    signals: list[SignalView]  # counted evidence: extract + verify supporting, failed facts out


@dataclass
class PageFacts:
    page: LoopPage
    data: CompetitorPage
    facts: list[tuple[CompetitorFact, QuoteMatch]]


@dataclass
class Found:
    """A competitor confirmed by at least one page on its own website."""

    name: str
    url: str | None
    segment: str | None
    geo: str
    pages: list[PageFacts] = field(default_factory=list)  # own pages first, then the rest


@dataclass
class Round:
    target: Target
    seeds: CompetitorSeeds
    trace: LoopTrace = field(default_factory=LoopTrace)
    pages: list[PageFacts] = field(default_factory=list)
    notes: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class ClaimView:
    id: int
    kind: str
    statement: str


@dataclass
class Stored:
    """One problem's competitors as written: db id -> name and their fact claims."""

    competitors: list[tuple[int, str, str | None]] = field(default_factory=list)  # id, name, seg
    claims: dict[int, list[ClaimView]] = field(default_factory=dict)  # competitor id -> claims
    prices: int = 0


# --- pure steps -----------------------------------------------------------------------------


def name_key(name: str) -> str:
    return " ".join(match_tokens(name))


def same_product(a: str, b: str) -> bool:
    """Turkish-aware name match: equal, or one name is the other plus more words."""
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    short, long = sorted((ka, kb), key=len)
    return ka == kb or long.startswith(f"{short} ")


def same_site(page_url: str, product_url: str | None) -> bool:
    """The page is on the product's own website (same domain or a subdomain either way)."""
    if not product_url:
        return False
    if "://" not in product_url:
        product_url = f"https://{product_url}"
    page, own = domain_of(page_url), domain_of(product_url)
    return bool(own) and (in_domain(page, own) or in_domain(own, page))


def numbers_in(text: str) -> set[float]:
    """Every number in ``text``, read with Turkish (1.250,50) and English (1,250.50) separators."""
    out: set[float] = set()
    for m in _NUMBER.finditer(text):
        raw = m.group()
        for candidate in (
            raw.replace(".", "").replace(",", "."),  # Turkish
            raw.replace(",", ""),  # English
        ):
            try:
                out.add(float(candidate))
            except ValueError:
                continue
    return out


def amount_in_quote(amount: float, quote: str) -> bool:
    return any(abs(n - amount) < 0.005 for n in numbers_in(quote))


def verify_facts(
    page: CompetitorPage, text: str, fuzzy_min_ratio: float, max_facts: int, notes: Counter[str]
) -> list[tuple[CompetitorFact, QuoteMatch]]:
    """Facts whose quote is on the page (and, for prices, whose amount is in the quote)."""
    kept: list[tuple[CompetitorFact, QuoteMatch]] = []
    for fact in page.facts[:max_facts]:
        notes["facts_proposed"] += 1
        match = verify_quote(fact.quote, text, fuzzy_min_ratio)
        if match is None:
            notes["facts_unverified"] += 1
            continue
        if fact.kind == "price" and (
            fact.amount is None or not amount_in_quote(fact.amount, match.quote)
        ):
            notes["prices_without_amount_in_quote"] += 1
            continue
        if any(m.char_start == match.char_start and f.kind == fact.kind for f, m in kept):
            notes["facts_repeated"] += 1
            continue
        notes["facts_verified"] += 1
        kept.append((fact, match))
    return kept


def resolve(pages: list[PageFacts], max_competitors: int) -> tuple[list[Found], Counter[str]]:
    """Competitors from the pages read: own-site pages create them, other pages attach by name."""
    notes: Counter[str] = Counter()
    found: list[Found] = []

    def match(name: str) -> Found | None:
        return next((f for f in found if same_product(f.name, name)), None)

    others: list[PageFacts] = []
    for pf in pages:
        name = pf.data.competitor_name
        if not name:
            notes["pages_without_product"] += 1
            continue
        if not same_site(pf.page.url, pf.data.product_url):
            others.append(pf)
            continue
        existing = match(name)
        if existing is None:
            if len(found) >= max_competitors:
                notes["over_cap_pages"] += 1
                continue
            existing = Found(name, pf.data.product_url, pf.data.segment, pf.data.geo)
            found.append(existing)
        existing.segment = existing.segment or pf.data.segment
        existing.pages.append(pf)
    for pf in others:
        existing = match(pf.data.competitor_name or "")
        if existing is None:
            notes["unconfirmed_pages"] += 1  # product never seen on its own site
            notes["unconfirmed_facts"] += len(pf.facts)
            continue
        existing.pages.append(pf)
    return found, notes


def humanize(key: str) -> str:
    return key.replace("_", " ")


def matrix_input(
    target: Target,
    competitors: list[tuple[int, str, str | None]],
    claims: dict[int, list[ClaimView]],
    cfg: CompetitorsDefaults,
) -> tuple[str, dict[int, int], dict[int, tuple[int, int]]]:
    """Prompt input with local numbers, plus ``signal number -> id`` and
    ``claim number -> (claim id, competitor number)``."""
    signal_no = {n: s.id for n, s in enumerate(target.signals, 1)}
    claim_no: dict[int, tuple[int, int]] = {}
    products = []
    for c_no, (competitor_id, name, segment) in enumerate(competitors, 1):
        listed = []
        for claim in claims.get(competitor_id, []):
            n = len(claim_no) + 1
            claim_no[n] = (claim.id, c_no)
            listed.append({"claim": n, "kind": claim.kind, "statement": claim.statement})
        products.append({"product": c_no, "name": name, "segment": segment, "claims": listed})
    payload = {
        "problem": {"name": target.name, "description": target.description},
        "signals": [
            {"signal": n, "type": s.type, "statement": s.statement}
            for n, s in enumerate(target.signals, 1)
        ],
        "fixed_dimensions": [{"key": k, "label": humanize(k)} for k in cfg.always_dimensions],
        "max_problem_dimensions": cfg.max_signal_dimensions,
        "products": products,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1), signal_no, claim_no


@dataclass
class Matrix:
    dimensions: list[dict[str, Any]]  # {key, label, from_signal_ids, fixed}
    cells: dict[str, dict[int, dict[str, Any]]]  # key -> competitor number -> {value, claim_no}
    notes: Counter[str]


def validate_matrix(
    draft: GapMatrixDraft,
    n_competitors: int,
    signal_no: dict[int, int],
    claim_no: dict[int, tuple[int, int]],
    claim_ok: dict[int, bool],
    cfg: CompetitorsDefaults,
) -> Matrix:
    """Dimensions from signals or the fixed list; cells citing a valid claim of their own
    competitor, everything else ``unknown``. ``claim_ok`` maps claim numbers to "passed
    entailment (or unchecked)"."""
    notes: Counter[str] = Counter()
    dims: list[dict[str, Any]] = []
    seen: set[str] = set()
    fixed = set(cfg.always_dimensions)
    added = 0
    for d in draft.dimensions:
        key = re.sub(r"[^a-z0-9_]+", "_", d.key.lower()).strip("_")
        if not key or key in seen:
            notes["dimensions_repeated"] += 1
            continue
        if key in fixed:
            dims.append({"key": key, "label": humanize(key), "from_signal_ids": [], "fixed": True})
            seen.add(key)
            continue
        from_ids = sorted({signal_no[n] for n in d.from_signals if n in signal_no})
        if not from_ids:
            notes["dimensions_without_signal"] += 1
            continue
        if added >= cfg.max_signal_dimensions:
            notes["dimensions_over_cap"] += 1
            continue
        added += 1
        dims.append({"key": key, "label": d.label, "from_signal_ids": from_ids, "fixed": False})
        seen.add(key)
    for key in cfg.always_dimensions:  # the fixed dimensions always apply
        if key not in seen:
            dims.append({"key": key, "label": humanize(key), "from_signal_ids": [], "fixed": True})
            seen.add(key)

    cells: dict[str, dict[int, dict[str, Any]]] = {d["key"]: {} for d in dims}
    for cell in draft.cells:
        key = re.sub(r"[^a-z0-9_]+", "_", cell.dimension.lower()).strip("_")
        if key not in cells or not 1 <= cell.competitor <= n_competitors:
            notes["cells_unknown_target"] += 1
            continue
        if cell.competitor in cells[key]:
            notes["cells_repeated"] += 1
            continue
        value, claim = cell.value, cell.claim
        if value != UNKNOWN:
            reason = None
            if claim is None:
                reason = "uncited"
            elif claim not in claim_no:
                reason = "unknown_claim"
            elif claim_no[claim][1] != cell.competitor:
                reason = "other_competitor"
            elif not claim_ok.get(claim, True):
                reason = "failed_entailment"
            if reason:
                notes[f"demoted_{reason}"] += 1
                value, claim = UNKNOWN, None
        else:
            claim = None
        cells[key][cell.competitor] = {"value": value, "claim_no": claim}
    for row in cells.values():
        for c_no in range(1, n_competitors + 1):
            if c_no not in row:
                notes["cells_missing"] += 1
                row[c_no] = {"value": UNKNOWN, "claim_no": None}
    return Matrix(dims, cells, notes)


def cited_claims(draft: GapMatrixDraft, claim_no: dict[int, tuple[int, int]]) -> list[int]:
    """Database ids of the claims non-unknown cells cite (for entailment before validation)."""
    return sorted(
        {claim_no[c.claim][0] for c in draft.cells if c.value != UNKNOWN and c.claim in claim_no}
    )


# --- database steps -------------------------------------------------------------------------


def reset(session: Session, run_id: int) -> None:
    """Remove everything a previous competitors run wrote."""
    session.execute(delete(GapMatrix).where(GapMatrix.run_id == run_id))
    session.execute(delete(Competitor).where(Competitor.run_id == run_id))
    session.execute(delete(Excerpt).where(Excerpt.run_id == run_id, Excerpt.stage == STAGE))
    session.execute(
        delete(Document).where(Document.run_id == run_id, Document.origin == "competitor")
    )
    delete_stage_claims(session, run_id, STAGE)
    session.execute(delete(Query).where(Query.run_id == run_id, Query.intent == "competitor"))
    clear_entailment(session, run_id, STAGE)


def load_targets(ctx: RunContext) -> list[Target]:
    with ctx.db() as session:
        clusters = session.scalars(
            select(ProblemCluster)
            .where(ProblemCluster.run_id == ctx.run_id, ProblemCluster.shortlisted.is_(True))
            .order_by(ProblemCluster.id)
        ).all()
        wanted = {
            c.id: [
                i
                for i in cluster_signal_ids(c)
                if i not in set(verification(c).get("excluded_signal_ids", []))
            ]
            for c in clusters
        }
        ids = sorted({i for v in wanted.values() for i in v})
        rows = {
            s.id: SignalView(s.id, s.type, s.statement, e.quote, e.translation)
            for s, e in session.execute(
                select(Signal, Excerpt)
                .join(Excerpt, Signal.excerpt_id == Excerpt.id)
                .where(Signal.id.in_(ids))
            )
        }
    return [
        Target(c.id, c.name, c.description, [rows[i] for i in wanted[c.id] if i in rows])
        for c in clusters
    ]


def write_round(
    session: Session,
    run_id: int,
    r: Round,
    found: list[Found],
    pack: MarketPack,
) -> Stored:
    stored = Stored()
    now = datetime.now(UTC)
    for f in found:
        competitor = Competitor(
            run_id=run_id,
            problem_id=r.target.id,
            name=f.name,
            url=f.url,
            segment=f.segment,
            geo=f.geo,
            pricing=[],
        )
        session.add(competitor)
        session.flush()
        claims: list[ClaimView] = []
        pricing: list[dict[str, Any]] = []
        for pf in f.pages:
            page = pf.page
            doc = (
                loop_page_document(page.page, pack)
                if page.page is not None
                else loop_snippet_document(page.hit.as_search_hit(), pack, now)
            )
            doc, _ = store_document(
                session, run_id, doc, origin="competitor", problem_id=r.target.id
            )
            for fact, match in pf.facts:
                excerpt = write_excerpt(
                    session, run_id, doc.id, page.source, match, fact.translation, stage=STAGE
                )
                claim = add_fact(
                    session,
                    run_id,
                    fact.statement,
                    [excerpt.id],
                    stage=STAGE,
                    meta={
                        "competitor_id": competitor.id,
                        "kind": fact.kind,
                        "page_kind": pf.data.page_kind,
                    },
                )
                claims.append(ClaimView(claim.id, fact.kind, fact.statement))
                if fact.kind == "price" and fact.amount is not None:
                    observed = doc.fetched_at or now
                    pricing.append(
                        {
                            "amount": fact.amount,
                            "currency": fact.currency,
                            "period": fact.period or "unknown",
                            "plan_name": fact.plan_name,
                            "observed_at": observed.isoformat(),
                            "claim_id": claim.id,
                        }
                    )
        competitor.pricing = pricing
        stored.prices += len(pricing)
        stored.competitors.append((competitor.id, f.name, f.segment))
        stored.claims[competitor.id] = claims
    return stored


def write_matrix(
    session: Session,
    run_id: int,
    target: Target,
    stored: Stored,
    matrix: Matrix,
    claim_no: dict[int, tuple[int, int]],
) -> list[dict[str, Any]]:
    """Store the matrix (database ids) and one gap inference per uncovered dimension."""
    ids = [c[0] for c in stored.competitors]
    cells: dict[str, dict[str, dict[str, Any]]] = {}
    gaps: list[dict[str, Any]] = []
    n = len(ids)
    for d in matrix.dimensions:
        row = matrix.cells[d["key"]]
        cells[d["key"]] = {
            str(ids[c_no - 1]): {
                "value": cell["value"],
                "claim_id": claim_no[cell["claim_no"]][0] if cell["claim_no"] else None,
            }
            for c_no, cell in sorted(row.items())
        }
        known = [c for c in cells[d["key"]].values() if c["value"] != UNKNOWN]
        if known and not any(c["value"] == "yes" for c in known):
            claim = add_inference(
                session,
                run_id,
                f"None of the {n} existing products reviewed fully covers: {d['label']}.",
                [c["claim_id"] for c in known],
                stage=STAGE,
                meta={"problem_id": target.id, "dimension": d["key"], "gap": True},
            )
            gaps.append({"dimension": d["key"], "claim_id": claim.id})
    session.add(
        GapMatrix(
            run_id=run_id,
            problem_id=target.id,
            matrix={
                "dimensions": matrix.dimensions,
                "competitor_ids": ids,
                "cells": cells,
                "gaps": gaps,
            },
        )
    )
    return gaps


# --- stage ----------------------------------------------------------------------------------


def seeds_input(
    target: Target, plan: ResearchPlan, pack: MarketPack, cfg: CompetitorsDefaults
) -> str:
    complaints = [s for s in target.signals if s.type == "tool_complaint"]
    payload = {
        "market": {"country": pack.country, "language": pack.language},
        "industry": plan.request.industry,
        "problem": {"name": target.name, "description": target.description},
        "max_suggestions": cfg.model_seed_names,
        "quotes": [
            {"type": s.type, "quote": s.quote, "translation": s.translation}
            for s in (complaints + [s for s in target.signals if s.type != "tool_complaint"])[:20]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def loop_goal(
    target: Target,
    seeds: CompetitorSeeds,
    plan: ResearchPlan,
    pack: MarketPack,
    cfg: CompetitorsDefaults,
) -> str:
    goal = {
        "market": {"country": pack.country, "language": pack.language},
        "industry": plan.request.industry,
        "problem": {"name": target.name, "description": target.description},
        "what_users_say": [s.statement for s in target.signals[:10]],
        "products_named_in_evidence": seeds.from_signals,
        "products_suggested_unverified": seeds.suggested[: cfg.model_seed_names],
        "max_products": cfg.max_competitors,
        "tasks": [
            "For each product, read a page on its own website (home or product page), its "
            "pricing page if any, a feature page about this workflow, and one user review or "
            "complaint page.",
            "Also search for the workflow itself to find products not listed here.",
        ],
    }
    return json.dumps(goal, ensure_ascii=False, indent=1)


def facts_input(target: Target, known: list[str], page: LoopPage, text: str, max_facts: int) -> str:
    head = {
        "problem": {"name": target.name, "description": target.description},
        "known_products": known,
        "max_facts": max_facts,
    }
    meta = {"url": page.url, "domain": page.hit.domain, "title": page.hit.title or ""}
    return (
        f"# Context\n{json.dumps(head, ensure_ascii=False, indent=1)}\n\n"
        f"# Page\n{json.dumps(meta, ensure_ascii=False, indent=1)}\n\n"
        f"# Text\n{text}"
    )


class Competitors:
    name = STAGE

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.competitors
        fuzzy = ctx.defaults.extract.fuzzy_min_ratio
        plan = load_run_plan(ctx)
        prompts = {
            p: load_prompt(p)
            for p in ("competitor_seeds", "competitors_loop", "competitor_facts", "gap_matrix")
        }

        with ctx.db.begin() as session:
            reset(session, ctx.run_id)
        targets = load_targets(ctx)
        with ctx.db() as session:
            known_queries = set(
                session.scalars(select(Query.text).where(Query.run_id == ctx.run_id)).all()
            )
        allowed = [s.domain for s in ctx.pack.sources if s.category in cfg.source_categories]

        def research(target: Target) -> Round:
            try:
                seeds = ctx.llm.parse(
                    ModelTier.FAST,
                    prompts["competitor_seeds"],
                    seeds_input(target, plan, ctx.pack, cfg),
                    CompetitorSeeds,
                    stage=self.name,
                    max_output_tokens=cfg.seed_output_tokens,
                ).output
            except BudgetExceeded:
                raise
            except LLMError:
                seeds = CompetitorSeeds(from_signals=[], suggested=[])
            r = Round(target, seeds)

            def on_page(page: LoopPage) -> str:
                text = page.text[: cfg.fact_chars]
                known = sorted({p.data.competitor_name for p in r.pages if p.data.competitor_name})
                try:
                    data = ctx.llm.parse(
                        ModelTier.FAST,
                        prompts["competitor_facts"],
                        facts_input(target, known, page, text, cfg.max_facts_per_page),
                        CompetitorPage,
                        stage=self.name,
                        max_output_tokens=cfg.max_output_tokens,
                    ).output
                except BudgetExceeded:
                    raise
                except LLMError:
                    r.notes["facts_call_failed"] += 1
                    return "could not read facts from this page"
                facts = verify_facts(data, text, fuzzy, cfg.max_facts_per_page, r.notes)
                r.pages.append(PageFacts(page, data, facts))
                if not data.competitor_name:
                    return f"not about one product ({data.page_kind})"
                own = " (its own site)" if same_site(page.url, data.product_url) else ""
                return f"{len(facts)} verified facts about {data.competitor_name}{own}"

            r.trace = run_loop(
                ctx,
                stage=self.name,
                prompt=prompts["competitors_loop"],
                goal=loop_goal(target, seeds, plan, ctx.pack, cfg),
                budget=cfg,
                allowed_domains=allowed,
                known_urls=(),
                known_queries=known_queries,
                on_page=on_page,
            )
            return r

        with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
            rounds = list(pool.map(research, targets))

        stored: dict[int, Stored] = {}
        resolved: dict[int, Counter[str]] = {}
        with ctx.db.begin() as session:
            store_searches(
                session,
                ctx.run_id,
                [(r.target.id, r.trace) for r in rounds],
                intent="competitor",
                lang=ctx.pack.language,
                provider=ctx.search.provider.name,
            )
            for r in rounds:
                found, notes = resolve(r.pages, cfg.max_competitors)
                resolved[r.target.id] = notes
                stored[r.target.id] = write_round(session, ctx.run_id, r, found, ctx.pack)

        # Matrix drafts (in parallel), entailment of the cited claims, then validation.
        inputs = {
            r.target.id: matrix_input(
                r.target, stored[r.target.id].competitors, stored[r.target.id].claims, cfg
            )
            for r in rounds
        }

        def draft(target: Target) -> GapMatrixDraft | None:
            if not stored[target.id].competitors:
                return None
            try:
                return ctx.llm.parse(
                    ModelTier.ANALYSIS,
                    prompts["gap_matrix"],
                    inputs[target.id][0],
                    GapMatrixDraft,
                    stage=self.name,
                    max_output_tokens=cfg.matrix_output_tokens,
                ).output
            except BudgetExceeded:
                raise
            except LLMError:
                return None

        with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
            drafts = dict(zip([t.id for t in targets], pool.map(draft, targets), strict=True))

        entailment: dict[str, int] = {}
        if cfg.entail_cells:
            cited = [
                i
                for t in targets
                if drafts[t.id] is not None
                for i in cited_claims(drafts[t.id], inputs[t.id][2])
            ]
            entailment = entail_pending(ctx, cited, stage=self.name)

        per_problem = []
        with ctx.db.begin() as session:
            verdicts = dict(
                session.execute(
                    select(Claim.id, Claim.entailment).where(
                        Claim.run_id == ctx.run_id, Claim.stage == self.name
                    )
                ).all()
            )
            for r in rounds:
                t, s = r.target, stored[r.target.id]
                _, signal_no, claim_no = inputs[t.id]
                d = drafts[t.id] or GapMatrixDraft(dimensions=[], cells=[])
                claim_ok = {n: not failed(verdicts.get(cid)) for n, (cid, _) in claim_no.items()}
                matrix = validate_matrix(d, len(s.competitors), signal_no, claim_no, claim_ok, cfg)
                gaps = write_matrix(session, ctx.run_id, t, s, matrix, claim_no)
                values = Counter(c["value"] for row in matrix.cells.values() for c in row.values())
                per_problem.append(
                    {
                        "id": t.id,
                        "name": t.name,
                        "seeds_from_signals": len(r.seeds.from_signals),
                        "seeds_suggested": len(r.seeds.suggested),
                        "stop_reason": r.trace.stop_reason,
                        "searches": len(r.trace.searches),
                        "pages_read": len(r.trace.pages),
                        "competitors": len(s.competitors),
                        "facts_verified": sum(len(c) for c in s.claims.values()),
                        "prices": s.prices,
                        "matrix_drafted": drafts[t.id] is not None,
                        "cells": dict(values),
                        "demoted": sum(
                            v for k, v in matrix.notes.items() if k.startswith("demoted_")
                        ),
                        "gaps": len(gaps),
                        "unconfirmed_pages": resolved[t.id]["unconfirmed_pages"],
                    }
                )

        metrics = competitor_metrics(rounds, per_problem, entailment)
        input_hash = cache_key(
            {
                "targets": [(t.id, [s.id for s in t.signals]) for t in targets],
                "plan": plan.model_dump(mode="json"),
                "config": cfg.model_dump(mode="json"),
                "prompts": [p.ref for p in prompts.values()],
            }
        )
        return StageResult(metrics=metrics, input_hash=input_hash)


def competitor_metrics(
    rounds: list[Round], per_problem: list[dict[str, Any]], entailment: dict[str, int]
) -> dict[str, Any]:
    notes: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    cells: Counter[str] = Counter()
    for r in rounds:
        notes.update(r.notes)
        rejections.update(r.trace.rejections)
    for p in per_problem:
        cells.update(p["cells"])
    return {
        "problems": len(rounds),
        "competitors": sum(p["competitors"] for p in per_problem),
        "pages_read": sum(p["pages_read"] for p in per_problem),
        "searches": sum(p["searches"] for p in per_problem),
        "facts_proposed": notes["facts_proposed"],
        "facts_verified": notes["facts_verified"],
        "facts_unverified": notes["facts_unverified"],
        "prices": sum(p["prices"] for p in per_problem),
        "prices_without_amount_in_quote": notes["prices_without_amount_in_quote"],
        "cells": dict(cells),
        "demoted_cells": sum(p["demoted"] for p in per_problem),
        "gaps": sum(p["gaps"] for p in per_problem),
        "stop_reasons": dict(Counter(r.trace.stop_reason for r in rounds)),
        "rejections": dict(rejections),
        "entailment": entailment,
        "per_problem": per_problem,
    }
