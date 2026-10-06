"""verify stage (plan §4, §7; agent-modules.md §4.2): second-round evidence per shortlisted problem.

Per shortlisted cluster, a bounded loop (agents/loop.py) searches and reads pages the run does not
have yet: more independent first-hand reports, counter-evidence (is it already solved? only one
vendor's claim?), and the official source of any regulatory obligation. Each page is read with
``prompts/verify_extract.md``: the extract rules plus the problem, and a ``stance`` per signal.
Quotes are verified like extract's. ``supports`` signals join the problem's evidence,
``counter`` signals are stored (``Signal.meta.counter``) but never count, ``unrelated`` ones are
dropped.

Then the problem's key claims (its strongest extract facts) and every new supporting fact are
entailment-checked (evidence/entailment.py); failed facts stop counting. Evidence strength is
recomputed over the remaining signals with the new independence groups, and Gate 1 is checked
again, plus a minimum of supported key claims. A problem that now fails is no longer shortlisted;
its ``gate_trace`` gets ``verify_*`` entries saying why.

Gate 1's own columns (``signal_ids``, ``evidence_strength``, ``strength``) are never changed: the
second round is recorded in ``ProblemCluster.verification`` (read through evidence/clusters.py),
so re-running shortlist or verify is idempotent.
"""

import json
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from signalforge.agents.loop import LoopPage, LoopTrace, run_loop, store_searches
from signalforge.config import ShortlistDefaults, VerifyDefaults
from signalforge.db.models import (
    Claim,
    Document,
    Excerpt,
    IndependenceGroup,
    ProblemCluster,
    Query,
    Signal,
)
from signalforge.domain.evidence import VerifyExtractionBatch
from signalforge.domain.plan import ResearchPlan
from signalforge.evidence.claims import delete_stage_claims, fact_ids_by_excerpt
from signalforge.evidence.clusters import pick_quotes
from signalforge.evidence.dedup import DocText, find_duplicates
from signalforge.evidence.documents import (
    collected_texts,
    loop_page_document,
    loop_snippet_document,
    store_document,
)
from signalforge.evidence.entailment import clear_entailment, entail_pending, failed
from signalforge.evidence.extraction import (
    KeptSignal,
    SourceText,
    extract_document,
    write_signals,
)
from signalforge.evidence.independence import (
    same_author_groups,
    same_quote_groups,
    source_units,
)
from signalforge.evidence.strength import UNLISTED, SignalRef, SourceDoc, evidence_strength
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult, load_run_plan
from signalforge.pipeline.stages.cluster import SignalItem, load_evidence
from signalforge.prompts import load_prompt
from signalforge.providers.cache import cache_key
from signalforge.providers.llm import ModelTier

VERIFY_RULE_PREFIX = "verify_"
SUPPORTED = ("supported", "partial")


@dataclass(frozen=True)
class Problem:
    id: int
    name: str
    description: str
    signals: list[SignalItem]  # extract signals, id order
    strength_before: dict[str, Any]
    inference_id: int | None

    @property
    def has_regulatory(self) -> bool:
        return any(s.type == "regulatory" for s in self.signals)


@dataclass
class Round:
    """One problem's loop and what its pages yielded, before anything is stored."""

    problem: Problem
    trace: LoopTrace
    kept: list[tuple[LoopPage, list[KeptSignal]]] = field(default_factory=list)
    notes: Counter[str] = field(default_factory=Counter)


# --- pure steps -----------------------------------------------------------------------------


def offered_domains(pack: MarketPack, cfg: VerifyDefaults, regulatory: bool) -> list[str]:
    """Registry domains the loop may restrict a search to (`site:`)."""
    categories = set(cfg.source_categories) | (
        set(cfg.official_categories) if regulatory else set()
    )
    return [s.domain for s in pack.sources if s.category in categories]


def missing_categories(
    problem: Problem, docs: dict[int, SourceDoc], cfg: VerifyDefaults
) -> list[str]:
    have = {docs[s.document_id].source_category or UNLISTED for s in problem.signals}
    return [c for c in cfg.source_categories if c not in have]


def problem_block(problem: Problem) -> str:
    """The problem as the extraction prompt sees it (prepended to extract's input)."""
    payload = {"name": problem.name, "description": problem.description}
    return f"# Problem under verification\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n\n"


def problem_goal(
    problem: Problem,
    docs: dict[int, SourceDoc],
    units: dict[int, int],
    plan: ResearchPlan,
    pack: MarketPack,
    cfg: VerifyDefaults,
    tier_weights: dict[str, float],
) -> str:
    """The loop goal: the problem, its strongest evidence so far, what is missing, the tasks."""
    quotes = [
        {
            "signal_id": s.id,
            "document_id": s.document_id,
            "first_hand": s.first_hand,
            "tier": docs[s.document_id].quality_tier,
            "verified": "exact",
            "snippet_only": docs[s.document_id].snippet_only,
            "published": None,
        }
        for s in problem.signals
    ]
    by_id = {s.id: s for s in problem.signals}
    top = [by_id[q["signal_id"]] for q in pick_quotes(quotes, units, tier_weights, cfg.top_signals)]
    sources = len({units.get(s.document_id, s.document_id) for s in problem.signals})
    categories = Counter(docs[s.document_id].source_category or UNLISTED for s in problem.signals)
    tasks = [
        "Find more independent, first-hand reports of this problem from businesses, preferably "
        "from the missing source categories (forum threads, job ads, complaints about tools).",
        "Look for counter-evidence: a common tool that already solves this for these businesses, "
        "or signs that the problem is only one vendor's marketing claim.",
    ]
    if problem.has_regulatory:
        tasks.append(
            "The evidence mentions a regulatory obligation: find the official source (government "
            "or official gazette page) that states it."
        )
    goal = {
        "market": {"country": pack.country, "language": pack.language},
        "industry": plan.request.industry,
        "problem": {"name": problem.name, "description": problem.description},
        "evidence_so_far": {
            "signals": len(problem.signals),
            "independent_sources": sources,
            "source_categories": dict(categories.most_common()),
            "strongest": [
                {
                    "type": s.type,
                    "statement": s.statement,
                    "first_hand": s.first_hand,
                    "submarket": s.submarket,
                    "source": docs[s.document_id].source_category or UNLISTED,
                }
                for s in top
            ],
        },
        "missing_source_categories": missing_categories(problem, docs, cfg),
        "tasks": tasks,
    }
    return json.dumps(goal, ensure_ascii=False, indent=1)


def key_claim_ids(
    problem: Problem,
    facts: dict[int, int],
    docs: dict[int, SourceDoc],
    units: dict[int, int],
    k: int,
    tier_weights: dict[str, float],
) -> list[int]:
    """The problem's ``k`` strongest extract facts: first-hand, high tier, distinct sources."""
    quotes = [
        {
            "signal_id": s.id,
            "document_id": s.document_id,
            "first_hand": s.first_hand,
            "tier": docs[s.document_id].quality_tier,
            "verified": "exact",
            "snippet_only": docs[s.document_id].snippet_only,
            "published": None,
            "fact": facts[s.excerpt_id],
        }
        for s in problem.signals
        if s.excerpt_id in facts
    ]
    return [q["fact"] for q in pick_quotes(quotes, units, tier_weights, k)]


def regate(
    score: float,
    sources: int,
    key_supported: int,
    key_total: int,
    gate: ShortlistDefaults,
    cfg: VerifyDefaults,
) -> list[dict[str, Any]]:
    """Gate 1 checked again on the verified evidence, plus the key-claim check."""
    need = min(cfg.min_key_claims_supported, key_total)
    return [
        {
            "rule": f"{VERIFY_RULE_PREFIX}min_strength",
            "value": score,
            "threshold": gate.min_strength,
            "passed": score >= gate.min_strength,
        },
        {
            "rule": f"{VERIFY_RULE_PREFIX}min_independent_sources",
            "value": sources,
            "threshold": gate.min_independent_sources,
            "passed": sources >= gate.min_independent_sources,
        },
        {
            "rule": f"{VERIFY_RULE_PREFIX}min_key_claims_supported",
            "value": key_supported,
            "threshold": need,
            "passed": key_supported >= need,
        },
    ]


def gate1_trace(trace: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The trace without verify's entries: what shortlist decided."""
    return [t for t in trace if not str(t["rule"]).startswith(VERIFY_RULE_PREFIX)]


# --- database steps -------------------------------------------------------------------------


def reset(session: Session, run_id: int) -> None:
    """Remove everything a previous verify run wrote, and restore the Gate-1 decisions."""
    verify_docs = session.scalars(
        select(Document.id).where(Document.run_id == run_id, Document.origin == "verify")
    ).all()
    if verify_docs:
        session.execute(
            delete(IndependenceGroup).where(
                IndependenceGroup.run_id == run_id,
                IndependenceGroup.document_ids.overlap(list(verify_docs)),
            )
        )
    session.execute(delete(Excerpt).where(Excerpt.run_id == run_id, Excerpt.stage == "verify"))
    session.execute(delete(Document).where(Document.id.in_(verify_docs)))
    delete_stage_claims(session, run_id, "verify")
    session.execute(delete(Query).where(Query.run_id == run_id, Query.intent == "verify"))
    clear_entailment(session, run_id, "verify")
    for c in session.scalars(select(ProblemCluster).where(ProblemCluster.run_id == run_id)):
        trace = gate1_trace(c.gate_trace or [])
        if c.verification is not None or len(trace) != len(c.gate_trace or []):
            c.gate_trace = trace
            c.shortlisted = bool(trace) and all(t["passed"] for t in trace)
            c.verification = None


def load_problems(ctx: RunContext) -> list[Problem]:
    signals, _, _ = load_evidence(ctx)
    by_id = {s.id: s for s in signals}
    with ctx.db() as session:
        clusters = session.scalars(
            select(ProblemCluster)
            .where(ProblemCluster.run_id == ctx.run_id, ProblemCluster.shortlisted.is_(True))
            .order_by(ProblemCluster.id)
        ).all()
    return [
        Problem(
            id=c.id,
            name=c.name,
            description=c.description,
            signals=[by_id[i] for i in sorted(c.signal_ids) if i in by_id],
            strength_before=c.strength or {},
            inference_id=c.claim_id,
        )
        for c in clusters
    ]


@dataclass
class Written:
    """What one problem's round stored."""

    supporting: list[tuple[int, int]] = field(default_factory=list)  # (signal id, fact id)
    counter: list[tuple[int, int]] = field(default_factory=list)
    document_ids: set[int] = field(default_factory=set)


def write_round(
    session: Session, run_id: int, r: Round, pack: MarketPack, texts: dict[int, str]
) -> Written:
    written = Written()
    now = datetime.now(UTC)
    for page, kept in r.kept:
        doc = (
            loop_page_document(page.page, pack)
            if page.page is not None
            else loop_snippet_document(page.hit.as_search_hit(), pack, now)
        )
        stored, _ = store_document(session, run_id, doc, origin="verify", problem_id=r.problem.id)
        written.document_ids.add(stored.id)
        texts.setdefault(stored.id, page.text)
        for item in kept:
            counter = item.signal.stance == "counter"
            (pair,) = write_signals(
                session,
                run_id,
                [replace(item, document_id=stored.id)],
                stage="verify",
                meta=lambda _, c=counter: {
                    "origin": "verify",
                    "problem_id": r.problem.id,
                    "counter": c,
                },
            )
            (written.counter if counter else written.supporting).append(pair)
    return written


def write_independence(
    session: Session,
    ctx: RunContext,
    verify_texts: dict[int, str],
    problem_docs: set[int],
) -> dict[str, int]:
    """Duplicate, same-author and same-quote groups that involve a verify document (collected
    documents' groups were written by dedupe and extract)."""
    verify_ids = set(verify_texts)
    if not verify_ids:
        return {}
    compare = {**collected_texts(ctx, sorted(problem_docs - verify_ids)), **verify_texts}
    docs = {
        d.id: d.domain
        for d in session.scalars(select(Document).where(Document.id.in_(list(compare))))
    }
    groups = find_duplicates(
        [DocText(i, docs[i], t) for i, t in sorted(compare.items()) if i in docs],
        ctx.defaults.dedupe,
    )
    authors: dict[int, set[str]] = {}
    quotes: dict[int, set[str]] = {}
    for doc_id, h, quote in session.execute(
        select(Excerpt.document_id, Excerpt.author_hash, Excerpt.quote).where(
            Excerpt.run_id == ctx.run_id
        )
    ):
        if h is not None:
            authors.setdefault(doc_id, set()).add(h)
        quotes.setdefault(doc_id, set()).add(quote)
    groups += same_author_groups(authors)
    groups += same_quote_groups(quotes, ctx.defaults.dedupe.same_quote_min_words)
    counts: Counter[str] = Counter()
    for g in groups:
        if verify_ids & set(g.document_ids):
            session.add(
                IndependenceGroup(run_id=ctx.run_id, rule=g.rule, document_ids=g.document_ids)
            )
            counts[g.rule] += 1
    return dict(counts)


def load_source_docs(session: Session, run_id: int) -> tuple[dict[int, SourceDoc], dict[int, int]]:
    docs = {
        d.id: SourceDoc(d.id, d.source_category, d.quality_tier, d.snippet_only, d.published_at)
        for d in session.scalars(select(Document).where(Document.run_id == run_id))
    }
    groups = session.scalars(
        select(IndependenceGroup.document_ids).where(IndependenceGroup.run_id == run_id)
    ).all()
    return docs, source_units(docs, groups)


# --- stage ----------------------------------------------------------------------------------


class Verify:
    name = "verify"

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.verify
        if ctx.settings.author_hash_salt is None:
            raise ValueError("AUTHOR_HASH_SALT is not set in .env (see .env.example)")
        salt = ctx.settings.author_hash_salt.get_secret_value()
        plan = load_run_plan(ctx)
        loop_prompt = load_prompt("verify_loop")
        extract_prompt = load_prompt("verify_extract")
        tiers = ctx.defaults.strength.tier_weights

        with ctx.db.begin() as session:
            reset(session, ctx.run_id)
        problems = load_problems(ctx)
        _, docs, units = load_evidence(ctx)
        with ctx.db() as session:
            known_urls = set(
                session.scalars(
                    select(Document.canonical_url).where(Document.run_id == ctx.run_id)
                ).all()
            )
            known_queries = set(
                session.scalars(select(Query.text).where(Query.run_id == ctx.run_id)).all()
            )

        def verify_one(problem: Problem) -> Round:
            context = problem_block(problem)

            def propose(prompt_input: str) -> tuple[VerifyExtractionBatch, bool]:
                result = ctx.llm.parse(
                    ModelTier.FAST,
                    extract_prompt,
                    context + prompt_input,
                    VerifyExtractionBatch,
                    stage=self.name,
                    max_output_tokens=ctx.defaults.extract.max_output_tokens,
                )
                return result.output, result.cache_hit

            r = Round(problem, LoopTrace())

            def on_page(page: LoopPage) -> str:
                hit = page.hit
                doc = SourceText(
                    document_id=0,  # assigned when the round is stored
                    domain=hit.domain,
                    title=(page.page.title if page.page else None) or hit.title,
                    source_category=hit.category,
                    quality_tier=hit.tier,
                    triage_label=None,
                    source=page.source,
                    text=page.text,
                    page_author=page.page.author if page.page else None,
                )
                found = extract_document(doc, plan, ctx.pack, ctx.defaults.extract, propose, salt)
                r.notes.update(found.notes)
                kept = [k for k in found.signals if k.signal.stance != "unrelated"]
                r.notes["unrelated_dropped"] += len(found.signals) - len(kept)
                if kept:
                    r.kept.append((page, kept))
                stances = Counter(k.signal.stance for k in kept)
                return (
                    f"{stances['supports']} supporting and {stances['counter']} counter "
                    "signals kept"
                )

            r.trace = run_loop(
                ctx,
                stage=self.name,
                prompt=loop_prompt,
                goal=problem_goal(problem, docs, units, plan, ctx.pack, cfg, tiers),
                budget=cfg,
                allowed_domains=offered_domains(ctx.pack, cfg, problem.has_regulatory),
                known_urls=known_urls,
                known_queries=known_queries,
                on_page=on_page,
            )
            return r

        with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
            rounds = list(pool.map(verify_one, problems))

        # Store every round with one writer, in problem order (deterministic ids).
        written: dict[int, Written] = {}
        verify_texts: dict[int, str] = {}
        problem_docs = {s.document_id for p in problems for s in p.signals}
        with ctx.db.begin() as session:
            store_searches(
                session,
                ctx.run_id,
                [(r.problem.id, r.trace) for r in rounds],
                intent="verify",
                lang=ctx.pack.language,
                provider=ctx.search.provider.name,
            )
            for r in rounds:
                written[r.problem.id] = write_round(session, ctx.run_id, r, ctx.pack, verify_texts)
            groups_written = write_independence(session, ctx, verify_texts, problem_docs)
            facts = fact_ids_by_excerpt(session, ctx.run_id, "extract")

        # Entailment: each problem's key claims and every new supporting fact, in one pass.
        keys = {
            p.id: key_claim_ids(p, facts, docs, units, cfg.key_claims_per_problem, tiers)
            for p in problems
        }
        to_check = [i for ids in keys.values() for i in ids] + [
            fact for w in written.values() for _, fact in w.supporting
        ]
        entailment = entail_pending(ctx, to_check, stage=self.name)

        per_problem = []
        with ctx.db.begin() as session:
            verdicts = dict(
                session.execute(
                    select(Claim.id, Claim.entailment).where(Claim.id.in_(to_check or [0]))
                ).all()
            )
            all_docs, all_units = load_source_docs(session, ctx.run_id)
            clusters = {
                c.id: c
                for c in session.scalars(
                    select(ProblemCluster).where(ProblemCluster.run_id == ctx.run_id)
                )
            }
            now = datetime.now(UTC)
            for r in rounds:
                p, w = r.problem, written[r.problem.id]
                extract_refs = [
                    (s.id, s.document_id, s.first_hand, facts.get(s.excerpt_id)) for s in p.signals
                ]
                new_refs = self._signal_refs(session, w.supporting)
                counted, excluded = [], []
                for signal_id, doc_id, first_hand, fact in extract_refs + new_refs:
                    if fact is not None and failed(verdicts.get(fact)):
                        excluded.append(signal_id)
                    else:
                        counted.append(SignalRef(doc_id, first_hand))
                after = evidence_strength(counted, all_docs, all_units, ctx.defaults.strength, now)
                key_verdicts = {i: verdicts.get(i) for i in keys[p.id]}
                key_supported = sum(v in SUPPORTED for v in key_verdicts.values())
                trace = regate(
                    after.score,
                    after.independent_sources,
                    key_supported,
                    len(keys[p.id]),
                    ctx.defaults.shortlist,
                    cfg,
                )
                passed = all(t["passed"] for t in trace)
                cluster = clusters[p.id]
                cluster.gate_trace = [*gate1_trace(cluster.gate_trace or []), *trace]
                cluster.shortlisted = passed
                before_sources = p.strength_before.get("independent_sources")
                cluster.verification = {
                    "passed": passed,
                    "signal_ids_added": [s for s, _ in w.supporting],
                    "counter_signal_ids": [s for s, _ in w.counter],
                    "excluded_signal_ids": sorted(excluded),
                    "document_ids_added": sorted(w.document_ids),
                    "strength_before": p.strength_before,
                    "strength_after": after.as_dict(),
                    "key_claim_ids": keys[p.id],
                    "key_claims": {str(i): v for i, v in key_verdicts.items()},
                    "loop": r.trace.summary(),
                    "extraction": dict(r.notes),
                }
                per_problem.append(
                    {
                        "id": p.id,
                        "name": p.name,
                        "stop_reason": r.trace.stop_reason,
                        "steps": r.trace.steps,
                        "searches": len(r.trace.searches),
                        "fetches": len(r.trace.pages) + len(r.trace.failed_fetches),
                        "new_signals": len(w.supporting),
                        "counter_signals": len(w.counter),
                        "excluded_signals": len(excluded),
                        "sources_before": before_sources,
                        "sources_after": after.independent_sources,
                        "strength_before": p.strength_before.get("score"),
                        "strength_after": after.score,
                        "key_claims_supported": f"{key_supported}/{len(keys[p.id])}",
                        "passed": passed,
                    }
                )

        metrics = verify_metrics(rounds, per_problem, entailment, groups_written)
        input_hash = cache_key(
            {
                "problems": [(p.id, [s.id for s in p.signals]) for p in problems],
                "plan": plan.model_dump(mode="json"),
                "config": cfg.model_dump(mode="json"),
                "gate": ctx.defaults.shortlist.model_dump(mode="json"),
                "prompts": [loop_prompt.ref, extract_prompt.ref, load_prompt("entailment").ref],
            }
        )
        return StageResult(metrics=metrics, input_hash=input_hash)

    @staticmethod
    def _signal_refs(
        session: Session, pairs: list[tuple[int, int]]
    ) -> list[tuple[int, int, bool, int | None]]:
        """(signal id, document id, first-hand, fact id) for signals verify stored."""
        if not pairs:
            return []
        fact_of = dict(pairs)
        rows = session.execute(
            select(Signal.id, Excerpt.document_id, Signal.first_hand)
            .join(Excerpt, Signal.excerpt_id == Excerpt.id)
            .where(Signal.id.in_(list(fact_of)))
            .order_by(Signal.id)
        ).all()
        return [(sid, doc_id, first_hand, fact_of[sid]) for sid, doc_id, first_hand in rows]


def verify_metrics(
    rounds: list[Round],
    per_problem: list[dict[str, Any]],
    entailment: dict[str, int],
    groups: dict[str, int],
) -> dict[str, Any]:
    notes: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    for r in rounds:
        notes.update(r.notes)
        rejections.update(r.trace.rejections)
    return {
        "problems": len(rounds),
        "passed": sum(p["passed"] for p in per_problem),
        "failed_verify": sum(not p["passed"] for p in per_problem),
        "searches": sum(p["searches"] for p in per_problem),
        "fetches": sum(p["fetches"] for p in per_problem),
        "new_signals": sum(p["new_signals"] for p in per_problem),
        "counter_signals": sum(p["counter_signals"] for p in per_problem),
        "unrelated_dropped": notes["unrelated_dropped"],
        "quotes_proposed": notes["quotes_proposed"],
        "quotes_unverified": notes["quotes_unverified"],
        "quotes_fuzzy": notes["quotes_fuzzy"],
        "stop_reasons": dict(Counter(r.trace.stop_reason for r in rounds)),
        "rejections": dict(rejections),
        "entailment": entailment,
        "independence_groups_added": groups,
        "per_problem": per_problem,
    }
