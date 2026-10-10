"""score stage (plan §8; agent-modules.md §8): one ScoreCard per opportunity, explainable from its
rule trace and cited claims.

1. **Knocked out** by Gate 2: a ``weak`` card with no factors, the knock-outs copied into the rule
   trace, and attractiveness, confidence and founder fit null (not assessed). No LLM call.
2. **Claim table** per passed opportunity: the problem's inference, its counted signal facts
   (job ads and price signals first, capped), the gap inferences it targets **and the competitor
   facts they derive from** (gap inferences are not facts, so citing them alone would always hit
   the uncited cap), competitor price and segment facts, buyer-role claims, and the economic
   model's assumption claims. With ``entail_cited`` every fact in the tables is
   entailment-checked first (``entail_pending``, stage ``score``); failed facts leave the table.
   The model sees local numbers 1..n.
3. **Rubric** (``score`` prompt, analysis tier): the model judges five factors; code caps a factor
   that cites no fact (``uncited_cap``) and ``competition_gap`` without a targeted gap
   (``no_gap_cap``), applies the WTP floor (distinct competitors with a price convertible to
   USD/month), and sets ``economic_impact`` (value midpoint) and ``market_breadth`` (not searched:
   a fixed level). The top ``k_judge_top_n`` by first-pass attractiveness get ``k_judges`` judges
   in total (``judge_index`` in the input, so cache keys differ); each factor takes the median.
4. **Confidence** from evidence strength, the hypothesis share of factor weight and the judges'
   spread; **founder fit** from a feasibility call (``founder_fit`` prompt) and rules;
   **category** by rules in order; the **experiment** tests the claim with the highest
   importance × uncertainty (``scoring/``).

A failed first rubric call leaves that opportunity without a card (the exit check reports it); a
failed extra judge only shrinks the sample; a failed feasibility call makes the fit ``stretch``.
"""

import json
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from statistics import median
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from signalforge.config import ScoreDefaults
from signalforge.db.models import Claim, Competitor, Opportunity, ProblemCluster, ScoreCard
from signalforge.domain.plan import ResearchPlan
from signalforge.domain.scoring import FeasibilityJudgment, RubricJudgments
from signalforge.evidence.clusters import current_strength
from signalforge.evidence.entailment import clear_entailment, entail_pending, failed
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult, load_run_plan
from signalforge.pipeline.stages.buyers import ROLES, SIGNAL, counted_signal_facts, load_targets
from signalforge.pipeline.stages.monetization import PRICE, evidence_order
from signalforge.prompts import load_prompt
from signalforge.providers.cache import cache_key
from signalforge.providers.llm import BudgetExceeded, LLMError, ModelTier
from signalforge.scoring.categories import CategoryInputs, categorize
from signalforge.scoring.economics import monthly_usd
from signalforge.scoring.experiments import (
    load_catalogue,
    pick_experiment,
    rank_claims,
    uncertainty_key,
)
from signalforge.scoring.scorer import (
    Cited,
    CodeFactors,
    Judge,
    code_factors,
    confidence,
    founder_fit,
    hypothesis_share,
    load_rubric,
    merge_judges,
    validate_judgments,
)

STAGE = "score"
# Where a table row comes from (shown to the model as ``about``), besides SIGNAL and PRICE.
PROBLEM, GAP, GAP_EVIDENCE, SEGMENT, ROLE, ASSUMPTION = (
    "problem", "gap", "gap_evidence", "competitor_segment", "buyer_role", "assumption",
)  # fmt: skip
PASSED_ENTAILMENT = ("supported", "partial")
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Row:
    id: int
    kind: str
    about: str
    statement: str
    entailment: str | None = None
    sourced: bool | None = None
    signal_type: str | None = None
    actor: str | None = None
    competitor: str | None = None  # competitor facts: whose page states it

    def cited(self) -> Cited:
        return Cited(self.id, self.kind, self.entailment, self.sourced)


@dataclass
class Target:
    """An opportunity as the scorer sees it."""

    id: int
    segment: str
    solution_angle: str
    status: str | None
    knockouts: list[dict[str, Any]]
    roles: dict[str, Any]  # role -> {role, basis}
    budget_owner: str  # none | hypothesis | evidence
    role_fact_ids: set[int]  # facts the buyer roles cite
    gap_ids: list[int]
    economic_model: dict[str, Any]
    channels: list[dict[str, Any]]
    market_breadth: dict[str, Any]
    problem: dict[str, str]
    strength: float
    supported_facts: int
    signal_facts: list[int]  # counted signal facts (uncapped), for entailment
    table: list[Row]
    floor: dict[str, list[int]]  # competitor -> qualifying price claim ids


# --- pure steps -----------------------------------------------------------------------------


def _claims_payload(table: list[Row]) -> list[dict[str, Any]]:
    out = []
    for n, row in enumerate(table, 1):
        item: dict[str, Any] = {"n": n, "kind": row.kind, "about": row.about}
        if row.about == SIGNAL:
            item |= {"signal_type": row.signal_type, "actor": row.actor}
        if row.competitor:
            item["competitor"] = row.competitor
        if row.entailment:
            item["entailment"] = row.entailment
        if row.sourced is not None:
            item["sourced"] = row.sourced
        out.append(item | {"statement": row.statement})
    return out


def _opportunity_payload(t: Target) -> dict[str, Any]:
    gap_numbers = [n for n, row in enumerate(t.table, 1) if row.about == GAP]
    return {
        "segment": t.segment,
        "solution_angle": t.solution_angle,
        "roles": t.roles,
        "channels": [{k: c.get(k) for k in ("kind", "name", "cited")} for c in t.channels],
        "gap_claims": gap_numbers,
    }


def rubric_input(t: Target, plan: ResearchPlan, pack: MarketPack, judge_index: int) -> str:
    """Rubric and market first; the opportunity, its claim table and the judge index last."""
    rubric = load_rubric()
    payload = {
        "rubric": rubric["factors"],
        "market": {"country": pack.country, "language": pack.language},
        "industry": plan.request.industry,
        "problem": t.problem,
        "opportunity": _opportunity_payload(t),
        "claims": _claims_payload(t.table),
        "judge_index": judge_index,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def feasibility_input(t: Target, plan: ResearchPlan, pack: MarketPack) -> str:
    founder = plan.request.founder
    payload = {
        "founder": founder.model_dump(exclude={"min_customer_value_usd_month"}),
        "market": {"country": pack.country, "language": pack.language},
        "industry": plan.request.industry,
        "problem": t.problem,
        "opportunity": {k: v for k, v in _opportunity_payload(t).items() if k != "gap_claims"},
        "claims": _claims_payload(t.table),
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def barrier_facts(f: FeasibilityJudgment, table: list[Row], notes: Counter[str]) -> list[int]:
    """Database ids of the facts the barrier citations name; other citations are dropped."""
    numbered = dict(enumerate(table, 1))
    ids: list[int] = []
    for n in f.barrier_claim_ids:
        row = numbered.get(n)
        if row is None:
            notes["invalid_citations"] += 1
        elif row.kind == "fact" and row.id not in ids:
            ids.append(row.id)
    return ids


def _testable(row: Row) -> str:
    """The claim as an experiment states it. An assumption keeps ``name = range unit`` and drops
    its rationale, which cites another prompt's local claim numbers."""
    if row.kind == "assumption":
        return row.statement.partition(": ")[0]
    return row.statement


def knocked_out_card(t: Target) -> dict[str, Any]:
    trace = [
        {"rule": f"gate2.{k['rule']}", "fired": True, "inputs": {"detail": k.get("detail")}}
        for k in t.knockouts
    ]
    return {"factors": {}, "attractiveness": None, "confidence": None, "founder_fit": None,
            "category": "weak", "rule_trace": trace, "experiment": None}  # fmt: skip


def score_card(
    t: Target,
    judges: list[Judge],
    code: CodeFactors,
    feasibility: FeasibilityJudgment | None,
    barrier_ids: list[int],
    founder: dict[str, Any],
    cfg: ScoreDefaults,
) -> dict[str, Any]:
    merged = merge_judges(judges, code, cfg)
    claims = {r.id: r.cited() for r in t.table}
    share, by_factor, share_notes = hypothesis_share(
        merged.factors, claims, cfg.weights, t.role_fact_ids
    )
    conf, conf_entry = confidence(
        t.strength, share, by_factor, merged.max_spread, merged.judges, cfg, share_notes
    )
    em = t.economic_model
    ceiling = em["price_ceiling_usd_month"] if em.get("status") == "ok" else None
    fit, fit_entry = founder_fit(
        feasibility, barrier_ids, ceiling[1] if ceiling else None, founder, cfg
    )
    minimum = founder["min_customer_value_usd_month"]
    category, category_trace = categorize(
        CategoryInputs(
            supported_facts=t.supported_facts,
            budget_owner=t.budget_owner,
            price_ceiling_high=ceiling[1] if ceiling else None,
            min_customer_value=minimum,
            severity=merged.levels["severity"],
            competition_gap=merged.levels["competition_gap"],
            willingness_to_pay=merged.levels["willingness_to_pay"],
            attractiveness=merged.attractiveness,
            strength=round(t.strength, 2),
        ),
        cfg,
    )
    # A pilot price worth testing: the ceiling's low end, at least the founder's minimum.
    price = round(max(ceiling[0] if ceiling else 0.0, minimum))
    keys = {r.id: uncertainty_key(r.kind, r.entailment, r.sourced) for r in t.table}
    fields = {r.id: {"segment": t.segment, "statement": _testable(r), "price_usd": price}
              for r in t.table}  # fmt: skip
    cited = {name: f["claim_ids"] for name, f in merged.factors.items()}
    experiment = pick_experiment(
        rank_claims(cited, keys, cfg.weights, cfg.uncertainty), load_catalogue(), fields
    )
    return {
        "factors": merged.factors,
        "attractiveness": merged.attractiveness,
        "confidence": conf,
        "founder_fit": fit,
        "category": category,
        "rule_trace": [*merged.trace, conf_entry, fit_entry, *category_trace],
        "experiment": experiment,
    }


# --- database steps -------------------------------------------------------------------------


def reset(session: Session, run_id: int) -> None:
    """Remove the run's score cards and the entailment verdicts this stage wrote."""
    opportunity_ids = select(Opportunity.id).where(Opportunity.run_id == run_id)
    session.execute(delete(ScoreCard).where(ScoreCard.opportunity_id.in_(opportunity_ids)))
    clear_entailment(session, run_id, STAGE)


def _basis(ids: list[int], claims: dict[int, Claim]) -> str:
    kinds = {claims[i].kind for i in ids if i in claims and not failed(claims[i].entailment)}
    return "evidence" if kinds & {"fact", "inference"} else "hypothesis"


def load(ctx: RunContext, cfg: ScoreDefaults) -> list[Target]:
    """Every opportunity of the run with its claim table (passed ones only)."""
    run_id = ctx.run_id
    problems = load_targets(ctx)
    fx = ctx.pack.economic("usd_try").value
    with ctx.db() as session:
        claims = {c.id: c for c in session.scalars(select(Claim).where(Claim.run_id == run_id))}
        signal_facts = counted_signal_facts(session, run_id, problems)
        clusters = {
            c.id: c
            for c in session.scalars(select(ProblemCluster).where(ProblemCluster.run_id == run_id))
        }
        competitors = session.scalars(
            select(Competitor).where(Competitor.run_id == run_id).order_by(Competitor.id)
        ).all()
        opportunities = session.scalars(
            select(Opportunity).where(Opportunity.run_id == run_id).order_by(Opportunity.id)
        ).all()
        out = []
        for o in opportunities:
            cluster = clusters[o.problem_id]
            facts = signal_facts.get(o.problem_id, [])
            roles = o.buyer_roles or {}
            owner = roles.get("budget_owner")
            table: list[Row] = []
            floor: dict[str, list[int]] = {}
            if o.status == "passed":
                table, floor = _table(o, cluster, facts, competitors, claims, cfg, ctx.pack, fx)
            out.append(
                Target(
                    id=o.id,
                    segment=o.segment,
                    solution_angle=o.solution_angle,
                    status=o.status,
                    knockouts=list(o.knockouts or []),
                    roles={
                        name: {"role": r["role"], "basis": _basis(r.get("claim_ids") or [], claims)}
                        for name in ROLES
                        if (r := roles.get(name))
                    },  # fmt: skip
                    budget_owner="none" if owner is None else _basis(owner["claim_ids"], claims),
                    role_fact_ids={
                        i
                        for name in ROLES
                        if (r := roles.get(name))
                        for i in r.get("claim_ids") or []
                        if i in claims and claims[i].kind == "fact"
                    },
                    gap_ids=list(roles.get("gap_claim_ids") or []),
                    economic_model=dict(o.economic_model or {}),
                    channels=list((o.accessibility or {}).get("channels") or []),
                    market_breadth=dict(o.market_breadth or {}),
                    problem={"name": cluster.name, "description": cluster.description},
                    strength=current_strength(cluster),
                    supported_facts=sum(c.entailment in PASSED_ENTAILMENT for _, c in facts),
                    signal_facts=[c.id for _, c in facts],
                    table=table,
                    floor=floor,
                )
            )
    return out


def _table(
    o: Opportunity,
    cluster: ProblemCluster,
    facts: list[Any],
    competitors: Any,
    claims: dict[int, Claim],
    cfg: ScoreDefaults,
    pack: MarketPack,
    fx: float,
) -> tuple[list[Row], dict[str, list[int]]]:
    rows: list[Row] = []
    seen: set[int] = set()

    def add(claim_id: int | None, about: str, **kw: Any) -> None:
        c = claims.get(claim_id or -1)
        if c is None or c.id in seen or (c.kind == "fact" and failed(c.entailment)):
            return
        seen.add(c.id)
        sourced = (c.meta or {}).get("sourced") if c.kind == "assumption" else None
        rows.append(Row(c.id, c.kind, about, c.statement, c.entailment, sourced, **kw))

    add(cluster.claim_id, PROBLEM)
    ordered = sorted(facts, key=lambda sf: evidence_order(sf[0].type, sf[0].first_hand, sf[0].id))
    for s, fact in ordered[: cfg.max_claims_per_opportunity]:
        add(fact.id, SIGNAL, signal_type=s.type, actor=s.actor)
    roles = o.buyer_roles or {}
    names = {comp.id: comp.name for comp in competitors}

    def competitor(claim_id: int) -> str | None:
        return names.get((claims[claim_id].meta or {}).get("competitor_id"))

    for gap_id in roles.get("gap_claim_ids") or []:
        add(gap_id, GAP)
        for source in claims[gap_id].derived_from if gap_id in claims else []:
            if source in claims and claims[source].kind == "fact":
                add(source, GAP_EVIDENCE, competitor=competitor(source))
    floor: dict[str, list[int]] = {}
    own = [comp for comp in competitors if comp.problem_id == o.problem_id]
    for comp in own:
        for p in comp.pricing:
            claim = claims.get(p.get("claim_id") or -1)
            if claim is None or failed(claim.entailment):
                continue
            add(claim.id, PRICE, competitor=comp.name)
            amount = float(p.get("amount") or 0)
            usd = monthly_usd(amount, p.get("currency") or "", p.get("period"), pack.currency, fx)
            if usd is not None and claim.id not in floor.get(comp.name, []):
                floor.setdefault(comp.name, []).append(claim.id)
    own_ids = {comp.id for comp in own}
    for c in claims.values():
        meta = c.meta or {}
        if (
            c.stage == "competitors"
            and meta.get("kind") == "segment"
            and (meta.get("competitor_id") in own_ids)
        ):
            add(c.id, SEGMENT, competitor=competitor(c.id))
    for name in ROLES:
        role = roles.get(name)
        if role:
            for i in role.get("claim_ids") or []:
                add(i, ROLE)
            add(role.get("hypothesis_claim_id"), ROLE)
    for claim_id in ((o.economic_model or {}).get("inputs") or {}).values():
        add(claim_id, ASSUMPTION)
    return rows, floor


def write(session: Session, opportunity_id: int, card: dict[str, Any]) -> None:
    session.add(ScoreCard(opportunity_id=opportunity_id, **card))


# --- stage ----------------------------------------------------------------------------------


class Score:
    name = STAGE

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.score
        plan = load_run_plan(ctx)
        prompt, fit_prompt = load_prompt("score"), load_prompt("founder_fit")
        founder = plan.request.founder.model_dump()

        with ctx.db.begin() as session:
            reset(session, ctx.run_id)
        targets = load(ctx, cfg)
        entailment: dict[str, int] = {}
        if cfg.entail_cited:
            ids = {r.id for t in targets for r in t.table if r.kind == "fact"}
            ids |= {i for t in targets if t.status == "passed" for i in t.signal_facts}
            entailment = entail_pending(ctx, ids, stage=STAGE)
            targets = load(ctx, cfg)  # failed facts leave the tables
        passed = [t for t in targets if t.status == "passed"]
        codes = {
            t.id: code_factors(t.economic_model, t.market_breadth, bool(t.gap_ids), t.floor, cfg)
            for t in passed
        }
        notes: Counter[str] = Counter()

        def ask(schema: type[T], p: Any, text: str) -> T | None:
            try:
                return ctx.llm.parse(
                    ModelTier.ANALYSIS, p, text, schema, stage=self.name,
                    max_output_tokens=cfg.max_output_tokens,
                ).output  # fmt: skip
            except BudgetExceeded:
                raise
            except LLMError:
                return None

        def judge(t: Target, i: int) -> RubricJudgments | None:
            return ask(RubricJudgments, prompt, rubric_input(t, plan, ctx.pack, i))

        def feasible(t: Target) -> FeasibilityJudgment | None:
            return ask(FeasibilityJudgment, fit_prompt, feasibility_input(t, plan, ctx.pack))

        def pooled(fn: Callable[..., Any], *args: list[Any]) -> list[Any]:
            with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
                return list(pool.map(fn, *args))

        first = pooled(judge, passed, [0] * len(passed))
        feasibility = pooled(feasible, passed)
        judges: dict[int, list[Judge]] = {}
        for t, r in zip(passed, first, strict=True):
            if r is not None:
                table = [row.cited() for row in t.table]
                judges[t.id] = [validate_judgments(r, table, codes[t.id], cfg)]
        top = sorted(
            (t for t in passed if t.id in judges),
            key=lambda t: -merge_judges(judges[t.id], codes[t.id], cfg).attractiveness,
        )[: cfg.k_judge_top_n]
        extra = [(t, i) for t in top for i in range(1, cfg.k_judges)]
        answers = pooled(judge, [t for t, _ in extra], [i for _, i in extra])
        for (t, _), r in zip(extra, answers, strict=True):
            if r is None:
                notes["extra_judge_failures"] += 1
            else:
                table = [row.cited() for row in t.table]
                judges[t.id].append(validate_judgments(r, table, codes[t.id], cfg))

        cards: dict[int, dict[str, Any]] = {}
        for t in targets:
            if t.status == "knocked_out":
                cards[t.id] = knocked_out_card(t)
        for t, f in zip(passed, feasibility, strict=True):
            notes["feasibility_failures"] += f is None
            if t.id not in judges:
                notes["rubric_failures"] += 1
                continue
            barrier_ids = barrier_facts(f, t.table, notes) if f else []
            for j in judges[t.id]:
                notes.update(j.notes)
            cards[t.id] = score_card(t, judges[t.id], codes[t.id], f, barrier_ids, founder, cfg)
        with ctx.db.begin() as session:
            for opportunity_id, card in cards.items():
                write(session, opportunity_id, card)

        metrics = score_metrics(targets, cards, notes, len(passed) + len(extra), entailment)
        rubric = load_rubric()
        input_hash = cache_key(
            {
                "targets": [
                    (t.id, t.status, t.economic_model, t.budget_owner, t.gap_ids, t.strength,
                     [(r.id, r.entailment) for r in t.table])
                    for t in targets
                ],
                "founder": founder,
                "config": cfg.model_dump(mode="json"),
                "rubric": rubric,
                "experiments": [e.model_dump() for e in load_catalogue()],
                "prompts": [prompt.ref, fit_prompt.ref, load_prompt("entailment").ref],
            }
        )  # fmt: skip
        return StageResult(metrics=metrics, input_hash=input_hash)


def score_metrics(
    targets: list[Target],
    cards: dict[int, dict[str, Any]],
    notes: Counter[str],
    judge_calls: int,
    entailment: dict[str, int],
) -> dict[str, Any]:
    scored = [c for c in cards.values() if c["attractiveness"] is not None]
    capped: Counter[str] = Counter()
    spreads = []
    for c in scored:
        capped.update(name for name, f in c["factors"].items() if f["capped"])
        conf = next(e for e in c["rule_trace"] if e["rule"] == "confidence")
        if conf["inputs"]["max_spread"] is not None:
            spreads.append(conf["inputs"]["max_spread"])
    by_id = {t.id: t for t in targets}
    unchecked = {
        r.id for t in targets for r in t.table if r.kind == "fact" and r.entailment is None
    }
    return {
        "opportunities": len(targets),
        "cards": len(cards),
        "scored": len(scored),
        "knocked_out": sum(t.status == "knocked_out" for t in targets),
        "unjudged": sum(t.status not in ("passed", "knocked_out") for t in targets),
        "by_category": dict(Counter(c["category"] for c in cards.values())),
        "confidence": dict(Counter(c["confidence"] for c in scored)),
        "founder_fit": dict(Counter(c["founder_fit"] for c in scored)),
        "capped": dict(capped),
        "floors": sum(bool(c["factors"]["willingness_to_pay"].get("floor")) for c in scored),
        "missing_factors": notes["missing_factors"],
        "invalid_citations": notes["invalid_citations"],
        "judge_calls": judge_calls,
        "judge_spread_median": median(spreads) if spreads else None,
        "llm_failures": {
            "rubric": notes["rubric_failures"],
            "extra_judges": notes["extra_judge_failures"],
            "feasibility": notes["feasibility_failures"],
        },
        "entailment": entailment,
        "unchecked_facts": len(unchecked),
        "per_opportunity": [
            {
                "id": oid,
                "segment": by_id[oid].segment,
                "category": c["category"],
                "attractiveness": c["attractiveness"],
                "confidence": c["confidence"],
                "founder_fit": c["founder_fit"],
                "levels": {n: f["level"] for n, f in c["factors"].items()},
                "experiment": (c["experiment"] or {}).get("name"),
            }
            for oid, c in sorted(cards.items())
        ],
    }
