"""monetization stage (plan §4 Gate 2; agent-modules.md §7): what an opportunity is worth to one
customer per month, as a range over explicit, dated assumptions, and whether it can pay.

Per opportunity written by ``buyers`` (its problem still shortlisted):

1. **Claim table.** The problem's counted signal facts (job ads and price signals first, failed
   entailment out, capped) and its competitors' price facts. The model sees local numbers 1..n.
   The pack's economics references (wages, working hours) are listed by name.
2. **Value model.** One analysis-tier call picks a formula and a range per input, citing claims
   or a pack reference.
3. **Validation (code).** The formula's inputs must all be present with the formula's units and
   ``low ≤ high``. A cited pack reference supplies the value instead of the model: as-is when its
   unit matches, or, for ``loaded_hourly_cost``, a net monthly wage × the config's loaded-cost
   multiplier ÷ working hours. A USD money value is converted at the pack's dated ``usd_try``.
   Only a value from the pack is ``sourced``: the model's own ranges are estimates
   (``sourced: false``) even when they cite claims, since a job ad shows the work exists but
   states no hours; the citations are kept as context. One bad input
   invalidates the model (stored as ``status: invalid`` with the reasons).
4. **Numbers (code).** ``scoring/economics.py``: value per month (local, then USD at the dated
   rate), price ceiling = value × capture share, and the competitor price anchor (min / median of
   prices convertible to USD per month).
5. **WTP signals (deterministic).** The problem's competitor prices, ``labor_spend`` job ads and
   ``price_signal`` signals, each citing its fact claim.
6. **Gate 2.** ``no_budget_owner`` (buyers left it null) and ``value_below_minimum`` (price
   ceiling high < the founder's ``min_customer_value_usd_month``) → ``knocked_out`` with reasons;
   otherwise ``passed``. An opportunity without a valid model is judged on the first rule only.
"""

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from signalforge.config import MonetizationDefaults
from signalforge.db.models import Claim, Competitor, Opportunity, ResearchRun
from signalforge.domain.commercial import AssumptionDraft, ValueModelDraft
from signalforge.domain.plan import ResearchPlan
from signalforge.evidence.claims import add_assumption, delete_stage_claims
from signalforge.evidence.entailment import failed
from signalforge.packs import EconomicReference, MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import StageResult, load_run_plan
from signalforge.pipeline.stages.buyers import TableRow, counted_signal_facts, load_targets
from signalforge.prompts import load_prompt
from signalforge.providers.cache import cache_key
from signalforge.providers.llm import BudgetExceeded, LLMError, ModelTier
from signalforge.scoring.economics import (
    FORMULAS,
    Input,
    Interval,
    anchor,
    loaded_hourly_cost,
    monthly_usd,
    to_usd,
    value_local,
)

STAGE = "monetization"
SIGNAL, PRICE = "signal", "competitor_price"
FX_REFERENCE, HOURS_REFERENCE = "usd_try", "working_hours_per_month"
ROLE_NAMES = ("user", "buyer", "decision_maker", "economic_beneficiary", "budget_owner")


@dataclass(frozen=True)
class Target:
    """An opportunity as monetization sees it."""

    id: int
    problem_id: int
    segment: str
    solution_angle: str
    roles: dict[str, str | None]  # role -> title as named in the market
    has_budget_owner: bool


@dataclass(frozen=True)
class Price:
    claim_id: int
    competitor: str
    amount: float
    currency: str
    period: str | None
    plan_name: str | None


@dataclass
class ProblemEvidence:
    name: str
    description: str
    table: list[TableRow] = field(default_factory=list)
    prices: list[Price] = field(default_factory=list)
    wtp: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Economics:
    """The dated numbers code may use, from the pack and the config."""

    currency: str
    fx: EconomicReference  # local units per USD
    references: dict[str, EconomicReference]
    multiplier: Interval  # net wage -> employer cost
    capture: Interval
    run_date: date

    @classmethod
    def load(cls, pack: MarketPack, cfg: MonetizationDefaults, run_date: date) -> "Economics":
        fx = pack.economic(FX_REFERENCE)  # missing → KeyError: fail loudly
        return cls(
            currency=pack.currency,
            fx=fx,
            references={r.name: r for r in pack.economics},
            multiplier=Interval(cfg.loaded_cost_multiplier.low, cfg.loaded_cost_multiplier.high),
            capture=Interval(cfg.capture_share.low, cfg.capture_share.high),
            run_date=run_date,
        )


@dataclass
class Assumption:
    name: str
    value: Interval
    unit: str
    currency: str | None
    claim_ids: list[int]
    as_of: str | None
    source_url: str | None
    sourced: bool
    rationale: str
    pack_reference: str | None = None
    derivation: dict[str, Any] | None = None

    def meta(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "value_low": round(self.value.low, 4),
            "value_high": round(self.value.high, 4),
            "unit": self.unit,
            "currency": self.currency,
            "as_of": self.as_of,
            "source_url": self.source_url,
            "sourced": self.sourced,
            "pack_reference": self.pack_reference,
        }
        return out | ({"derivation": self.derivation} if self.derivation else {})

    def statement(self) -> str:
        lo, hi = (f"{v:,.2f}".rstrip("0").rstrip(".") for v in (self.value.low, self.value.high))
        span = lo if lo == hi else f"{lo}–{hi}"
        return f"{self.name} = {span} {self.unit}: {self.rationale}"


@dataclass
class Model:
    formula: str
    assumptions: list[Assumption]
    value_local: Interval
    value_usd: Interval
    ceiling_usd: Interval


@dataclass
class Checked:
    model: Model | None = None
    errors: list[str] = field(default_factory=list)
    notes: Counter[str] = field(default_factory=Counter)


# --- pure steps -----------------------------------------------------------------------------


def evidence_order(signal_type: str, first_hand: bool, signal_id: int) -> tuple[int, bool, int]:
    """Job ads (money already spent on the work) and price signals first, then first-hand."""
    rank = {"labor_spend": 0, "price_signal": 1}.get(signal_type, 2)
    return (rank, not first_hand, signal_id)


def analysis_input(
    target: Target, problem: ProblemEvidence, econ: Economics, plan: ResearchPlan
) -> str:
    """Prompt input: market, formulas and pack references first; the opportunity and its claim
    table (local numbers) last."""
    claims = []
    for n, row in enumerate(problem.table, 1):
        item: dict[str, Any] = {"n": n, "kind": row.kind, "about": row.about}
        if row.about == SIGNAL:
            item |= {"signal_type": row.signal_type, "actor": row.actor}
        claims.append(item | {"statement": row.statement})
    payload = {
        "market": {"country": plan.request.country, "currency": econ.currency},
        "formulas": {
            name: {i.name: i.expected_unit(econ.currency) for i in inputs}
            for name, inputs in FORMULAS.items()
        },
        "pack_references": [
            {
                "name": r.name,
                "value": r.value,
                "unit": r.unit,
                "as_of": r.as_of.isoformat(),
                "note": r.note,
            }
            for r in econ.references.values()
            if r.name != FX_REFERENCE
        ],  # fmt: skip
        "industry": plan.request.industry,
        "problem": {"name": problem.name, "description": problem.description},
        "opportunity": {
            "segment": target.segment,
            "solution_angle": target.solution_angle,
            "roles": target.roles,
        },
        "claims": claims,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _cite(numbers: list[int], table: dict[int, TableRow], notes: Counter[str]) -> list[int]:
    ids: list[int] = []
    for n in numbers:
        row = table.get(n)
        if row is None:
            notes["invalid_citations"] += 1
        elif row.id not in ids:
            ids.append(row.id)
    return ids


def from_reference(
    inp: Input, d: AssumptionDraft, ids: list[int], econ: Economics, notes: Counter[str]
) -> Assumption | None:
    """The input's value taken from the pack reference ``d`` names, or ``None`` when the
    reference is unknown or cannot supply this input (the model's own value is used then)."""
    ref = econ.references.get(d.pack_reference or "")
    if ref is None or ref.name == FX_REFERENCE:
        notes["unknown_pack_reference"] += 1
        return None
    expected = inp.expected_unit(econ.currency)
    base = {"name": inp.name, "unit": expected, "claim_ids": ids, "as_of": ref.as_of.isoformat(),
            "source_url": ref.source_url, "sourced": True, "rationale": d.rationale,
            "pack_reference": ref.name}  # fmt: skip
    if ref.unit == expected:
        return Assumption(value=Interval(ref.value, ref.value), currency=ref.currency, **base)
    hours = econ.references.get(HOURS_REFERENCE)
    if inp.name == "loaded_hourly_cost" and ref.unit == f"{econ.currency}/month" and hours:
        derivation = {
            "net_monthly_wage": ref.name,
            "multiplier": econ.multiplier.rounded(),
            "multiplier_source": "config: monetization.loaded_cost_multiplier",
            "hours_per_month": hours.name,
        }
        value = loaded_hourly_cost(ref.value, econ.multiplier, hours.value)
        return Assumption(value=value, currency=econ.currency, derivation=derivation, **base)
    notes["pack_reference_unit_mismatch"] += 1
    return None


def resolve_input(
    inp: Input,
    d: AssumptionDraft | None,
    table: dict[int, TableRow],
    econ: Economics,
    notes: Counter[str],
) -> Assumption | str:
    """The validated assumption for one formula input, or why it is invalid."""
    if d is None:
        return f"{inp.name}: missing"
    ids = _cite(d.claim_ids, table, notes)
    if d.pack_reference and (from_ref := from_reference(inp, d, ids, econ, notes)):
        return from_ref
    if not 0 <= d.low <= d.high:
        return f"{inp.name}: not a range 0 ≤ low ≤ high ({d.low}, {d.high})"
    value, currency, conversion = Interval(d.low, d.high), None, None
    if inp.per:
        currency = (d.currency or d.unit.split("/", 1)[0]).strip().upper()
        if currency not in (econ.currency, "USD") or d.unit != f"{currency}/{inp.per}":
            expected = f"{econ.currency}|USD/{inp.per}"
            return f"{inp.name}: unit {d.unit!r} ({d.currency}) is not {expected}"
        if currency == "USD":
            conversion = {"from": "USD", "rate": econ.fx.value, "as_of": econ.fx.as_of.isoformat(),
                          "source_url": econ.fx.source_url}  # fmt: skip
            value, currency = value * econ.fx.value, econ.currency
    elif d.unit != inp.unit:
        return f"{inp.name}: unit {d.unit!r} is not {inp.unit!r}"
    elif inp.unit == "fraction" and d.high > 1:
        return f"{inp.name}: a fraction above 1 ({d.high})"
    return Assumption(
        name=inp.name,
        value=value,
        unit=inp.expected_unit(econ.currency),
        currency=currency,
        claim_ids=ids,
        as_of=econ.run_date.isoformat() if inp.per else None,
        source_url=None,
        sourced=False,  # the model's estimate; citations are context, not the number
        rationale=d.rationale,
        derivation={"converted": conversion} if conversion else None,
    )


def validate_model(draft: ValueModelDraft, table: list[TableRow], econ: Economics) -> Checked:
    """Every formula input resolved (pack value over model value), then the numbers."""
    out = Checked()
    numbered = dict(enumerate(table, 1))
    by_name: dict[str, AssumptionDraft] = {}
    for a in draft.assumptions:
        if a.name in by_name or a.name not in {i.name for i in FORMULAS[draft.formula]}:
            out.notes["extra_assumptions"] += 1
        else:
            by_name[a.name] = a
    assumptions = []
    for inp in FORMULAS[draft.formula]:
        resolved = resolve_input(inp, by_name.get(inp.name), numbered, econ, out.notes)
        if isinstance(resolved, str):
            out.errors.append(resolved)
        else:
            assumptions.append(resolved)
    if out.errors:
        return out
    local = value_local(draft.formula, {a.name: a.value for a in assumptions})
    usd = to_usd(local, econ.fx.value)
    out.model = Model(draft.formula, assumptions, local, usd, usd * econ.capture)
    return out


def knockouts(target: Target, model: Model | None, min_usd_month: float) -> list[dict[str, str]]:
    """Gate 2 (plan §4)."""
    out = []
    if not target.has_budget_owner:
        out.append({"rule": "no_budget_owner", "detail": "buyers found no budget owner"})
    if model is not None and model.ceiling_usd.high < min_usd_month:
        out.append(
            {
                "rule": "value_below_minimum",
                "detail": f"price ceiling ≤ ${model.ceiling_usd.high:,.2f}/month < founder "
                f"minimum ${min_usd_month:,.2f}/month",
            }
        )
    return out


# --- database steps -------------------------------------------------------------------------


def reset(session: Session, run_id: int) -> None:
    """Clear what a previous monetization run wrote on the run's opportunities."""
    for o in session.scalars(select(Opportunity).where(Opportunity.run_id == run_id)):
        o.economic_model, o.wtp_signals, o.status, o.knockouts = {}, [], None, []
    delete_stage_claims(session, run_id, STAGE)


def load_opportunities(session: Session, run_id: int, problem_ids: set[int]) -> list[Target]:
    rows = session.scalars(
        select(Opportunity)
        .where(Opportunity.run_id == run_id, Opportunity.problem_id.in_(problem_ids))
        .order_by(Opportunity.id)
    ).all()
    return [
        Target(
            o.id,
            o.problem_id,
            o.segment,
            o.solution_angle,
            {r: (o.buyer_roles.get(r) or {}).get("role") for r in ROLE_NAMES},
            o.buyer_roles.get("budget_owner") is not None,
        )
        for o in rows
    ]


def problem_evidence(
    session: Session, run_id: int, cfg: MonetizationDefaults, ctx: RunContext
) -> dict[int, ProblemEvidence]:
    """Per shortlisted problem: claim table, competitor prices and WTP signals."""
    problems = load_targets(ctx)
    signal_facts = counted_signal_facts(session, run_id, problems)
    competitors = session.scalars(
        select(Competitor).where(Competitor.run_id == run_id).order_by(Competitor.id)
    ).all()
    price_ids = {p.get("claim_id") for c in competitors for p in c.pricing}
    price_claims = {
        c.id: c
        for c in session.scalars(select(Claim).where(Claim.id.in_([i for i in price_ids if i])))
    }
    out: dict[int, ProblemEvidence] = {}
    for t in problems:
        ev = ProblemEvidence(t.name, t.description)
        counted = sorted(
            signal_facts[t.id], key=lambda s: evidence_order(s[0].type, s[0].first_hand, s[0].id)
        )
        for s, fact in counted[: cfg.max_claims_per_opportunity]:
            ev.table.append(TableRow(fact.id, fact.kind, SIGNAL, fact.statement, s.type, s.actor))
            if s.type in ("labor_spend", "price_signal"):
                ev.wtp.append({"kind": s.type, "claim_id": fact.id, "note": s.actor or ""})
        for c in competitors:
            if c.problem_id != t.id:
                continue
            for p in c.pricing:
                claim = price_claims.get(p.get("claim_id") or -1)
                if claim is None or failed(claim.entailment) or not p.get("amount"):
                    continue
                price = Price(claim.id, c.name, float(p["amount"]), p.get("currency") or "",
                              p.get("period"), p.get("plan_name"))  # fmt: skip
                ev.prices.append(price)
                ev.table.append(TableRow(claim.id, claim.kind, PRICE, claim.statement))
                plan = f" ({price.plan_name})" if price.plan_name else ""
                ev.wtp.append(
                    {"kind": "competitor_price", "claim_id": claim.id,
                     "note": f"{c.name}{plan}: {price.amount:g} {price.currency} / "
                             f"{price.period or 'unknown'}"}
                )  # fmt: skip
        out[t.id] = ev
    return out


def competitor_anchor(prices: list[Price], econ: Economics) -> dict[str, Any] | None:
    converted = [
        (p, monthly_usd(p.amount, p.currency, p.period, econ.currency, econ.fx.value))
        for p in prices
    ]
    usable = [(p, v) for p, v in converted if v is not None]
    summary = anchor([v for _, v in usable])
    if summary is None:
        return None
    return summary | {
        "n": len(usable),
        "claim_ids": [p.claim_id for p, _ in usable],
        "per_user_prices": sum(p.period == "per_user_month" for p, _ in usable),
    }


def write(
    session: Session,
    run_id: int,
    target: Target,
    checked: Checked | None,
    evidence: ProblemEvidence,
    econ: Economics,
    min_usd_month: float,
) -> None:
    opp = session.get(Opportunity, target.id)
    assert opp is not None
    model = checked.model if checked else None
    if model is None:
        opp.economic_model = (
            {"status": "invalid", "errors": checked.errors} if checked else {"status": "llm_error"}
        )
    else:
        inputs = {}
        for a in model.assumptions:
            inputs[a.name] = add_assumption(
                session, run_id, a.statement(), stage=STAGE, meta=a.meta(), claim_ids=a.claim_ids
            ).id
        opp.economic_model = {
            "status": "ok",
            "formula": model.formula,
            "inputs": inputs,
            "value_local": model.value_local.rounded(),
            "currency": econ.currency,
            "value_usd_month": model.value_usd.rounded(),
            "fx": {"rate": econ.fx.value, "as_of": econ.fx.as_of.isoformat(),
                   "source_url": econ.fx.source_url},
            "capture_share": econ.capture.rounded(),
            "price_ceiling_usd_month": model.ceiling_usd.rounded(),
            "competitor_anchor_usd_month": competitor_anchor(evidence.prices, econ),
        }  # fmt: skip
    opp.wtp_signals = evidence.wtp
    opp.knockouts = knockouts(target, model, min_usd_month)
    opp.status = "knocked_out" if opp.knockouts else "passed"


# --- stage ----------------------------------------------------------------------------------


class Monetization:
    name = STAGE

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.defaults.monetization
        plan = load_run_plan(ctx)
        prompt = load_prompt("monetization")
        min_usd = plan.request.founder.min_customer_value_usd_month

        with ctx.db.begin() as session:
            reset(session, ctx.run_id)
            created = session.scalar(
                select(ResearchRun.created_at).where(ResearchRun.id == ctx.run_id)
            )
        econ = Economics.load(ctx.pack, cfg, created.date() if created else date.today())
        with ctx.db() as session:
            evidence = problem_evidence(session, ctx.run_id, cfg, ctx)
            targets = load_opportunities(session, ctx.run_id, set(evidence))

        def analyse(target: Target) -> ValueModelDraft | None:
            try:
                return ctx.llm.parse(
                    ModelTier.ANALYSIS,
                    prompt,
                    analysis_input(target, evidence[target.problem_id], econ, plan),
                    ValueModelDraft,
                    stage=self.name,
                    max_output_tokens=cfg.max_output_tokens,
                ).output
            except BudgetExceeded:
                raise
            except LLMError:
                return None

        with ThreadPoolExecutor(max_workers=max(cfg.concurrency, 1)) as pool:
            answers = list(pool.map(analyse, targets))

        checked: list[Checked | None] = [
            validate_model(a, evidence[t.problem_id].table, econ) if a else None
            for t, a in zip(targets, answers, strict=True)
        ]
        with ctx.db.begin() as session:
            for t, c in zip(targets, checked, strict=True):
                write(session, ctx.run_id, t, c, evidence[t.problem_id], econ, min_usd)
        with ctx.db() as session:
            stored = {
                o.id: o
                for o in session.scalars(
                    select(Opportunity).where(Opportunity.id.in_([t.id for t in targets]))
                )
            }
        metrics = monetization_metrics(targets, checked, stored)
        input_hash = cache_key(
            {
                "opportunities": [(t.id, t.problem_id, t.segment, t.roles) for t in targets],
                "plan": plan.model_dump(mode="json"),
                "config": cfg.model_dump(mode="json"),
                "economics": [r.model_dump(mode="json") for r in ctx.pack.economics],
                "prompts": [prompt.ref],
            }
        )
        return StageResult(metrics=metrics, input_hash=input_hash)


def monetization_metrics(
    targets: list[Target], checked: list[Checked | None], stored: dict[int, Opportunity]
) -> dict[str, Any]:
    notes: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    models: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    assumptions = [a for c in checked if c and c.model for a in c.model.assumptions]
    widths = []
    per_opportunity = []
    for t, c in zip(targets, checked, strict=True):
        o = stored[t.id]
        notes.update(c.notes if c else {})
        models["ok" if c and c.model else "invalid" if c else "llm_error"] += 1
        errors.update(e.split(":", 1)[0] for e in (c.errors if c else []))
        rules.update(k["rule"] for k in o.knockouts)
        if c and c.model and c.model.value_local.low > 0:
            widths.append(c.model.value_local.high / c.model.value_local.low)
        em = o.economic_model
        per_opportunity.append(
            {"id": t.id, "segment": t.segment, "status": o.status, "model": em.get("status"),
             "formula": em.get("formula"), "value_usd_month": em.get("value_usd_month"),
             "price_ceiling_usd_month": em.get("price_ceiling_usd_month"),
             "knockouts": [k["rule"] for k in o.knockouts]}
        )  # fmt: skip
    return {
        "opportunities": len(targets),
        "passed": sum(stored[t.id].status == "passed" for t in targets),
        "knocked_out": sum(stored[t.id].status == "knocked_out" for t in targets),
        "knockouts": dict(rules),
        "models": dict(models),
        "invalid_inputs": dict(errors),
        "assumptions": len(assumptions),
        "sourced_share": round(sum(a.sourced for a in assumptions) / len(assumptions), 2)
        if assumptions
        else 0.0,
        "pack_referenced": sum(a.pack_reference is not None for a in assumptions),
        "value_width_median": round(median(widths), 2) if widths else None,
        "with_price_anchor": sum(
            bool(stored[t.id].economic_model.get("competitor_anchor_usd_month")) for t in targets
        ),
        "invalid_citations": notes["invalid_citations"],
        "pack_reference_issues": notes["unknown_pack_reference"]
        + notes["pack_reference_unit_mismatch"],
        "per_opportunity": per_opportunity,
    }
