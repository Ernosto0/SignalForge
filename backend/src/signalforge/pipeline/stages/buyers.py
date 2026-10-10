"""buyers stage (plan §4; agent-modules.md §6): who would buy a solution to each problem.

Per shortlisted problem (after verify and competitors):

1. **Claim table.** The problem's counted signal facts (extract + verify, failed entailment out;
   job ads first, then first-hand signals, capped), its inference, its gap inferences, its
   competitors' ``segment`` facts, and the plan's ``buyer_hypotheses``, written once per run as
   hypothesis claims (``meta.origin = "plan"``) so they can be cited and stay labelled. The model
   sees local numbers 1..n, never database ids.
2. **Analysis.** One analysis-tier call proposes 1–3 opportunities (segment × solution angle),
   each with its buyer roles (user, buyer, decision maker, economic beneficiary, budget owner),
   reach channels and a ``breadth_hint``.
3. **Validation.** Citations outside the table are dropped and counted. A role left with neither
   a citation nor a hypothesis drops its opportunity; a null budget owner is kept (Gate 2 in
   ``monetization`` knocks it out). Gap citations must be this problem's gap inferences. Channels
   without a citation are kept as ``cited: false``. Near-duplicate segments collapse (first wins),
   then the list is capped.
4. **Role check.** Quote verification and fact entailment prove a fact states *its own*
   statement, not that it names a buyer role. Each role citing facts is checked as one claim
   ("A <role> approves purchases for this work: <problem>") against those facts' quotes with the
   ``entailment`` prompt. A role whose facts fail keeps only its non-fact citations and becomes a
   hypothesis; the rejected facts stay listed under ``rejected_claim_ids``.
5. **Rows.** One ``Opportunity`` per kept draft, and one hypothesis claim per role hypothesis
   (``meta.opportunity_id``, ``meta.role``) so the report can cite it. Market breadth is not
   searched yet: ``market_breadth = {hint, status: "not_searched"}``.
"""

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from signalforge.config import BuyersDefaults
from signalforge.db.models import Claim, Competitor, Opportunity, ProblemCluster, Signal
from signalforge.domain.commercial import BuyerAnalysis, RoleClaim
from signalforge.domain.plan import ResearchPlan
from signalforge.evidence.claims import add_hypothesis, delete_stage_claims, fact_ids_by_excerpt
from signalforge.evidence.clusters import cluster_signal_ids, verification
from signalforge.evidence.entailment import (
    ClaimEvidence,
    EntailmentBatch,
    check,
    failed,
    load_claim_evidence,
)
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult, load_run_plan
from signalforge.prompts import load_prompt
from signalforge.providers.cache import cache_key
from signalforge.providers.llm import BudgetExceeded, LLMError, ModelTier
from signalforge.queries import Deduper

STAGE = "buyers"
ROLES = ("user", "buyer", "decision_maker", "economic_beneficiary", "budget_owner")
PLAN_ORIGIN = "plan"
# Where a table row comes from (shown to the model as ``about``).
SIGNAL, PROBLEM, GAP, SEGMENT, PLAN_HYPOTHESIS = (
    "signal", "problem", "gap", "competitor_segment", "plan_hypothesis",
)  # fmt: skip
# What citing a fact for a role claims; checked against the facts' quotes (step 4).
ROLE_CLAIMS = {
    "user": "A {role} does this work: {problem}.",
    "buyer": "A {role} chooses and buys tools for this work: {problem}.",
    "decision_maker": "A {role} approves purchases for this work: {problem}.",
    "economic_beneficiary": "A {role} gains when this work gets faster or cheaper: {problem}.",
    "budget_owner": "A {role} pays for this work or the tools for it: {problem}.",
}


@dataclass(frozen=True)
class Target:
    """A shortlisted problem as buyer research sees it."""

    id: int
    name: str
    description: str
    claim_id: int | None  # the cluster's inference
    signal_ids: list[int]  # extract + verify supporting, verify-excluded out


@dataclass(frozen=True)
class TableRow:
    id: int  # database claim id
    kind: str  # fact | inference | hypothesis
    about: str
    statement: str
    signal_type: str | None = None
    actor: str | None = None


@dataclass
class Draft:
    """A validated opportunity, database ids throughout, ready to write."""

    segment: str
    solution_angle: str
    roles: dict[str, dict[str, Any] | None]  # role -> {role, claim_ids, hypothesis}
    basis: dict[str, str]  # role -> fact | inference | hypothesis
    gap_claim_ids: list[int]
    channels: list[dict[str, Any]]
    breadth_hint: str | None


@dataclass
class Validated:
    drafts: list[Draft] = field(default_factory=list)
    notes: Counter[str] = field(default_factory=Counter)


# --- pure steps -----------------------------------------------------------------------------


def signal_order(signal_type: str, first_hand: bool, signal_id: int) -> tuple[bool, bool, int]:
    """Job ads first (they name the role doing the work and whom it reports to), then
    first-hand signals, then the rest."""
    return (signal_type != "labor_spend", not first_hand, signal_id)


def analysis_input(
    target: Target, table: list[TableRow], plan: ResearchPlan, pack: MarketPack, cfg: BuyersDefaults
) -> str:
    """Prompt input: run context first, the problem's claim table (local numbers) last."""
    actors = list(dict.fromkeys(a for s in plan.submarkets for a in s.actors))
    claims = []
    for n, row in enumerate(table, 1):
        item: dict[str, Any] = {"n": n, "kind": row.kind, "about": row.about}
        if row.about == SIGNAL:
            item |= {"signal_type": row.signal_type, "actor": row.actor}
        claims.append(item | {"statement": row.statement})
    payload = {
        "market": {"country": pack.country, "language": pack.language},
        "industry": plan.request.industry,
        "target_customer": plan.request.target_customer,
        "actors": actors,
        "max_opportunities": cfg.max_opportunities_per_problem,
        "problem": {"name": target.name, "description": target.description},
        "claims": claims,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _basis(kinds: set[str]) -> str:
    if "fact" in kinds:
        return "fact"
    if "inference" in kinds:
        return "inference"
    return "hypothesis"


def _cite(numbers: list[int], table: dict[int, TableRow], notes: Counter[str]) -> list[int]:
    """Database ids of the cited table numbers; unknown numbers are counted and dropped."""
    ids: list[int] = []
    for n in numbers:
        row = table.get(n)
        if row is None:
            notes["invalid_citations"] += 1
        elif row.id not in ids:
            ids.append(row.id)
    return ids


def validate_role(
    rc: RoleClaim, table: dict[int, TableRow], notes: Counter[str]
) -> tuple[dict[str, Any], str] | None:
    """The role with database ids and its basis, or ``None`` when it has neither a valid
    citation nor a hypothesis."""
    ids = _cite(rc.claim_ids, table, notes)
    hypothesis = (rc.hypothesis or "").strip() or None
    if not ids and hypothesis is None:
        return None
    kinds = {row.kind for n, row in table.items() if n in rc.claim_ids}
    role = {"role": rc.role.strip(), "claim_ids": ids, "hypothesis": hypothesis}
    return role, _basis(kinds)


def validate_analysis(
    analysis: BuyerAnalysis, table: list[TableRow], cfg: BuyersDefaults
) -> Validated:
    """Drafts whose every role is cited or a hypothesis, citations mapped to database ids,
    segments deduplicated within the problem, capped at ``max_opportunities_per_problem``."""
    out = Validated()
    numbered = dict(enumerate(table, 1))
    dedupe = Deduper(cfg.segment_dup_ratio)
    for d in analysis.opportunities:
        segment = d.segment.strip()
        if not segment:
            out.notes["dropped_empty_segment"] += 1
            continue
        roles: dict[str, dict[str, Any] | None] = {}
        basis: dict[str, str] = {}
        valid = True
        for name in ROLES:
            rc = getattr(d.buyer_roles, name)
            if rc is None:  # only budget_owner may be null: kept for Gate 2
                roles[name] = None
                continue
            checked = validate_role(rc, numbered, out.notes)
            if checked is None:
                valid = False
                break
            roles[name], basis[name] = checked
        if not valid:
            out.notes["dropped_unsupported_role"] += 1
            continue
        gaps = []
        for n in d.gap_claim_ids:
            row = numbered.get(n)
            if row is None or row.about != GAP:
                out.notes["gap_citations_dropped"] += 1
            elif row.id not in gaps:
                gaps.append(row.id)
        channels = []
        for c in d.channels:
            ids = _cite(c.claim_ids, numbered, out.notes)
            channels.append({"kind": c.kind, "name": c.name, "claim_ids": ids, "cited": bool(ids)})
        if not dedupe.add(segment, None):
            out.notes["dropped_duplicate_segment"] += 1
            continue
        if len(out.drafts) >= cfg.max_opportunities_per_problem:
            out.notes["dropped_over_cap"] += 1
            continue
        out.drafts.append(
            Draft(segment, d.solution_angle.strip(), roles, basis, gaps, channels, d.breadth_hint)
        )
    return out


@dataclass(frozen=True)
class RoleCheck:
    """One role that cites facts, as the claim that citation makes."""

    draft: Draft
    name: str  # one of ROLES
    statement: str
    fact_ids: list[int]


def role_checks(
    targets: list[Target], validated: dict[int, Validated], kinds: dict[int, str]
) -> list[RoleCheck]:
    out = []
    for t in targets:
        for d in validated[t.id].drafts:
            for name, role in d.roles.items():
                facts = [i for i in role["claim_ids"] if kinds.get(i) == "fact"] if role else []
                if role and facts:
                    statement = ROLE_CLAIMS[name].format(role=role["role"], problem=t.name)
                    out.append(RoleCheck(d, name, statement, facts))
    return out


def apply_role_verdict(
    rc: RoleCheck, verdict: str | None, note: str | None, kinds: dict[int, str]
) -> bool:
    """Record the verdict on the role. A failed one drops the role's fact citations (kept as
    ``rejected_claim_ids``) and makes it a hypothesis. Returns whether the role was demoted."""
    role = rc.draft.roles[rc.name]
    assert role is not None
    role["entailment"], role["entailment_note"] = verdict, note
    if not failed(verdict):
        return False
    role["claim_ids"] = [i for i in role["claim_ids"] if i not in rc.fact_ids]
    role["rejected_claim_ids"] = rc.fact_ids
    role["hypothesis"] = role["hypothesis"] or rc.statement
    rc.draft.basis[rc.name] = _basis({kinds[i] for i in role["claim_ids"]})
    return True


def entail_roles(ctx: RunContext, checks: list[RoleCheck], kinds: dict[int, str]) -> dict[str, int]:
    """Check each role against the quotes of the facts it cites (fast tier, ``entailment``
    prompt); apply the verdicts to the drafts. A role the model skips stays unchecked."""
    if not checks:
        return {"checked": 0}
    ecfg = ctx.defaults.entailment
    prompt = load_prompt("entailment")
    fact_ids = {i for c in checks for i in c.fact_ids}
    with ctx.db() as session:
        quotes = {
            e.claim_id: e.quotes
            for e in load_claim_evidence(session, ctx.run_id, fact_ids, ecfg.max_quotes_per_claim)
        }
    items = [
        ClaimEvidence(
            n,
            c.statement,
            [q for i in c.fact_ids for q in quotes.get(i, [])][: ctx.defaults.buyers.role_quotes],
        )
        for n, c in enumerate(checks)
    ]

    def ask(prompt_input: str) -> tuple[EntailmentBatch, bool]:
        result = ctx.llm.parse(
            ModelTier.FAST,
            prompt,
            prompt_input,
            EntailmentBatch,
            stage=STAGE,
            max_output_tokens=ecfg.max_output_tokens,
        )
        return result.output, result.cache_hit

    verdicts, notes = check(items, ask, ecfg.batch_size)
    counts: Counter[str] = Counter()
    for n, c in enumerate(checks):
        judged = verdicts.get(n)
        verdict = judged.verdict if judged else None
        counts[verdict or "unchecked"] += 1
        counts["demoted"] += apply_role_verdict(c, verdict, judged.note if judged else None, kinds)
    notes.pop("missing", None)  # counted as unchecked
    return {"checked": len(checks), **counts, **notes}


# --- database steps -------------------------------------------------------------------------


def reset(session: Session, run_id: int) -> None:
    """Remove everything a previous buyers run wrote (score cards cascade)."""
    session.execute(delete(Opportunity).where(Opportunity.run_id == run_id))
    delete_stage_claims(session, run_id, STAGE)


def write_plan_hypotheses(session: Session, run_id: int, plan: ResearchPlan) -> None:
    for text in plan.buyer_hypotheses:
        add_hypothesis(session, run_id, text, stage=STAGE, meta={"origin": PLAN_ORIGIN})


def load_targets(ctx: RunContext) -> list[Target]:
    with ctx.db() as session:
        clusters = session.scalars(
            select(ProblemCluster)
            .where(ProblemCluster.run_id == ctx.run_id, ProblemCluster.shortlisted.is_(True))
            .order_by(ProblemCluster.id)
        ).all()
        return [
            Target(
                c.id,
                c.name,
                c.description,
                c.claim_id,
                [
                    i
                    for i in cluster_signal_ids(c)
                    if i not in set(verification(c).get("excluded_signal_ids", []))
                ],
            )
            for c in clusters
        ]


def claim_tables(
    session: Session, run_id: int, targets: list[Target], cfg: BuyersDefaults
) -> dict[int, list[TableRow]]:
    """Per problem: its inference, counted signal facts (capped), gap inferences, competitor
    segment facts, then the plan's buyer hypotheses. Facts that failed entailment are left out."""
    facts = {
        **fact_ids_by_excerpt(session, run_id, "extract"),
        **fact_ids_by_excerpt(session, run_id, "verify"),
    }
    claims = {
        c.id: c
        for c in session.scalars(select(Claim).where(Claim.run_id == run_id).order_by(Claim.id))
    }
    signal_ids = sorted({i for t in targets for i in t.signal_ids})
    signals = {s.id: s for s in session.scalars(select(Signal).where(Signal.id.in_(signal_ids)))}
    competitor_problem = dict(
        session.execute(
            select(Competitor.id, Competitor.problem_id).where(Competitor.run_id == run_id)
        ).all()
    )

    def row(c: Claim, about: str, **kw: Any) -> TableRow:
        return TableRow(c.id, c.kind, about, c.statement, **kw)

    plan_rows = [
        row(c, PLAN_HYPOTHESIS)
        for c in claims.values()
        if c.stage == STAGE and (c.meta or {}).get("origin") == PLAN_ORIGIN
    ]
    tables: dict[int, list[TableRow]] = {}
    for t in targets:
        table: list[TableRow] = []
        if t.claim_id is not None and t.claim_id in claims:
            table.append(row(claims[t.claim_id], PROBLEM))
        counted = []
        for sid in t.signal_ids:
            s = signals.get(sid)
            fact = claims.get(facts.get(s.excerpt_id, -1)) if s is not None else None
            if s is None or fact is None or failed(fact.entailment):
                continue
            counted.append((signal_order(s.type, s.first_hand, s.id), fact, s))
        counted.sort(key=lambda x: x[0])
        table += [
            row(fact, SIGNAL, signal_type=s.type, actor=s.actor)
            for _, fact, s in counted[: cfg.max_claims_per_problem]
        ]
        gaps, segments = [], []
        for c in claims.values():
            meta = c.meta or {}
            if c.stage != "competitors":
                continue
            if c.kind == "inference" and meta.get("gap") and meta.get("problem_id") == t.id:
                gaps.append(row(c, GAP))
            elif (
                c.kind == "fact"
                and meta.get("kind") == "segment"
                and competitor_problem.get(meta.get("competitor_id")) == t.id
                and not failed(c.entailment)
            ):
                segments.append(row(c, SEGMENT))
        tables[t.id] = table + gaps + segments + plan_rows
    return tables


def write_drafts(session: Session, run_id: int, problem_id: int, drafts: list[Draft]) -> None:
    """Opportunity rows, then a hypothesis claim per role hypothesis (derived from the role's
    citations, if any)."""
    for d in drafts:
        opp = Opportunity(
            run_id=run_id,
            problem_id=problem_id,
            segment=d.segment,
            solution_angle=d.solution_angle,
            buyer_roles={},
            accessibility={"channels": d.channels},
            market_breadth={"hint": d.breadth_hint, "status": "not_searched"},
        )
        session.add(opp)
        session.flush()
        roles: dict[str, Any] = {}
        for name, role in d.roles.items():
            if role is None:
                roles[name] = None
                continue
            claim_id = None
            if role["hypothesis"]:
                claim_id = add_hypothesis(
                    session,
                    run_id,
                    role["hypothesis"],
                    stage=STAGE,
                    claim_ids=role["claim_ids"],
                    meta={"opportunity_id": opp.id, "role": name},
                ).id
            roles[name] = {**role, "hypothesis_claim_id": claim_id}
        opp.buyer_roles = {**roles, "gap_claim_ids": d.gap_claim_ids}


# --- stage ----------------------------------------------------------------------------------


class Buyers:
    name = STAGE

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.buyers
        plan = load_run_plan(ctx)
        prompt = load_prompt("buyers")

        with ctx.db.begin() as session:
            reset(session, ctx.run_id)
            write_plan_hypotheses(session, ctx.run_id, plan)
        targets = load_targets(ctx)
        with ctx.db() as session:
            tables = claim_tables(session, ctx.run_id, targets, cfg)

        def analyse(target: Target) -> BuyerAnalysis | None:
            try:
                return ctx.llm.parse(
                    ModelTier.ANALYSIS,
                    prompt,
                    analysis_input(target, tables[target.id], plan, ctx.pack, cfg),
                    BuyerAnalysis,
                    stage=self.name,
                    max_output_tokens=cfg.max_output_tokens,
                ).output
            except BudgetExceeded:
                raise
            except LLMError:
                return None

        with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
            answers = dict(zip([t.id for t in targets], pool.map(analyse, targets), strict=True))

        validated = {
            t.id: validate_analysis(a, tables[t.id], cfg) if (a := answers[t.id]) else Validated()
            for t in targets
        }
        role_check: dict[str, int] = {}
        if cfg.entail_roles:
            kinds = {row.id: row.kind for rows in tables.values() for row in rows}
            role_check = entail_roles(ctx, role_checks(targets, validated, kinds), kinds)

        per_problem = []
        with ctx.db.begin() as session:
            for t in targets:
                answer, v = answers[t.id], validated[t.id]
                write_drafts(session, ctx.run_id, t.id, v.drafts)
                reason = "ok"
                if answer is None:
                    reason = "llm_error"
                elif not v.drafts:
                    reason = "no_valid_opportunities"
                per_problem.append(
                    {
                        "id": t.id,
                        "name": t.name,
                        "claims": len(tables[t.id]),
                        "proposed": len(answer.opportunities) if answer else 0,
                        "opportunities": len(v.drafts),
                        "reason": reason,
                        "notes": dict(v.notes),
                    }
                )

        metrics = {**buyer_metrics(validated, per_problem), "role_check": role_check}
        input_hash = cache_key(
            {
                "targets": [(t.id, t.claim_id, t.signal_ids) for t in targets],
                "plan": plan.model_dump(mode="json"),
                "config": cfg.model_dump(mode="json"),
                "entailment": ctx.defaults.entailment.model_dump(mode="json"),
                "prompts": [prompt.ref, load_prompt("entailment").ref],
            }
        )
        return StageResult(metrics=metrics, input_hash=input_hash)


def buyer_metrics(
    validated: dict[int, Validated], per_problem: list[dict[str, Any]]
) -> dict[str, Any]:
    notes: Counter[str] = Counter()
    basis: Counter[str] = Counter()
    drafts = [d for v in validated.values() for d in v.drafts]
    for v in validated.values():
        notes.update(v.notes)
    for d in drafts:
        basis.update(d.basis.values())
    channels = sum(len(d.channels) for d in drafts)
    return {
        "problems": len(per_problem),
        "opportunities": len(drafts),
        "roles": dict(basis),
        "budget_owners": sum(d.roles["budget_owner"] is not None for d in drafts),
        "channels": channels,
        "channels_cited": sum(c["cited"] for d in drafts for c in d.channels),
        "channels_per_opportunity": round(channels / len(drafts), 2) if drafts else 0.0,
        "gap_targeted": sum(bool(d.gap_claim_ids) for d in drafts),
        "dropped": {k.removeprefix("dropped_"): v for k, v in notes.items() if "dropped_" in k},
        "invalid_citations": notes["invalid_citations"],
        "gap_citations_dropped": notes["gap_citations_dropped"],
        "llm_failures": sum(p["reason"] == "llm_error" for p in per_problem),
        "per_problem": per_problem,
    }
