"""Claim tables of the report: which claims each section's writer may cite (agent-modules.md §9).

The pool of an opportunity is the claim table its score card was judged on (``score.load``: failed
facts are already out) plus the claims behind its buyer channels. Each section picks rows from the
pool by where they come from (``about``) and gets extra allowed sources (the stored economic model,
card levels, the experiment). Rows are numbered 1..n per section when shown to the model.
"""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from signalforge.config import ReportDefaults
from signalforge.db.models import Claim, Document, Excerpt, Opportunity, ScoreCard
from signalforge.evidence.entailment import failed
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.stages.buyers import ROLES
from signalforge.pipeline.stages.score import (
    ASSUMPTION,
    GAP,
    GAP_EVIDENCE,
    PRICE,
    PROBLEM,
    ROLE,
    SEGMENT,
    SIGNAL,
    Row,
    Target,
    load,
)
from signalforge.reporting.schema import ClaimView, ExcerptView
from signalforge.reporting.validator import claim_text

CHANNEL = "channel"
CATEGORY_RANK = {"strong": 0, "interesting": 1, "competitive": 2}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass
class OppData:
    """An opportunity with its card and claim pool, as plain data."""

    id: int
    problem_id: int
    segment: str
    solution_angle: str
    status: str | None
    knockouts: list[dict[str, Any]]
    buyer_roles: dict[str, Any]
    economic_model: dict[str, Any]
    accessibility: dict[str, Any]
    card: dict[str, Any] | None
    target: Target
    pool: list[Row]

    @property
    def category(self) -> str | None:
        return self.card["category"] if self.card else None


def load_opportunities(ctx: RunContext) -> list[OppData]:
    """Every opportunity of the run with its card and claim pool (pool only for passed ones)."""
    targets = {t.id: t for t in load(ctx, ctx.defaults.score)}
    with ctx.db() as session:
        opps = session.scalars(
            select(Opportunity).where(Opportunity.run_id == ctx.run_id).order_by(Opportunity.id)
        ).all()
        cards = {
            c.opportunity_id: {
                "factors": c.factors, "attractiveness": c.attractiveness,
                "confidence": c.confidence, "founder_fit": c.founder_fit,
                "category": c.category, "rule_trace": c.rule_trace, "experiment": c.experiment,
            }
            for c in session.scalars(
                select(ScoreCard).where(ScoreCard.opportunity_id.in_([o.id for o in opps]))
            )
        }  # fmt: skip
        claims = {c.id: c for c in session.scalars(select(Claim).where(Claim.run_id == ctx.run_id))}
        out = []
        for o in opps:
            pool = list(targets[o.id].table)
            seen = {r.id for r in pool}
            for ch in (o.accessibility or {}).get("channels") or []:
                for i in ch.get("claim_ids") or []:
                    c = claims.get(i)
                    if c and i not in seen and not (c.kind == "fact" and failed(c.entailment)):
                        seen.add(i)
                        pool.append(Row(i, c.kind, CHANNEL, c.statement, c.entailment))
            out.append(
                OppData(
                    id=o.id, problem_id=o.problem_id, segment=o.segment,
                    solution_angle=o.solution_angle, status=o.status,
                    knockouts=list(o.knockouts or []), buyer_roles=dict(o.buyer_roles or {}),
                    economic_model=dict(o.economic_model or {}),
                    accessibility=dict(o.accessibility or {}), card=cards.get(o.id),
                    target=targets[o.id], pool=pool,
                )
            )  # fmt: skip
    return out


def select_full(opps: list[OppData], n: int) -> list[OppData]:
    """Top ``n`` opportunities by category (strong > interesting > competitive), then
    attractiveness, then confidence. Only scored cards in those categories qualify."""
    eligible = [
        o for o in opps if o.card and o.category in CATEGORY_RANK and o.card["attractiveness"]
    ]
    eligible.sort(
        key=lambda o: (
            CATEGORY_RANK[o.category or ""],
            -(o.card or {})["attractiveness"],
            CONFIDENCE_RANK.get((o.card or {})["confidence"], 3),
            o.id,
        )
    )
    return eligible[:n]


def eligible_count(opps: list[OppData]) -> int:
    return sum(1 for o in opps if o.card and o.category in CATEGORY_RANK)


# --- section tables -------------------------------------------------------------------------


def _roles_of(o: OppData) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for name in (*ROLES, "budget_owner"):
        role = o.buyer_roles.get(name)
        if role:
            for i in [*(role.get("claim_ids") or []), role.get("hypothesis_claim_id")]:
                if i is not None:
                    out.setdefault(i, []).append(name)
    return out


def _balanced(groups: list[list[Row]], cap: int) -> list[Row]:
    """Rows of all groups, deduplicated, at most ``cap``: each group gets an equal share first."""
    share = max(cap // max(len(groups), 1), 1)
    picked: list[Row] = []
    seen: set[int] = set()
    for g in groups:
        for r in g[:share]:
            if r.id not in seen:
                seen.add(r.id)
                picked.append(r)
    for g in groups:
        for r in g:
            if len(picked) >= cap:
                return picked
            if r.id not in seen:
                seen.add(r.id)
                picked.append(r)
    return picked[:cap]


def _fmt(value: Any) -> str:
    if isinstance(value, list):
        return "–".join(_fmt(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k} {_fmt(v)}" for k, v in value.items() if k != "claim_ids")
    return str(value)


def economic_extra(model: dict[str, Any]) -> list[str]:
    if model.get("status") != "ok":
        return []
    keys = ("formula", "currency", "value_local", "value_usd_month", "price_ceiling_usd_month",
            "capture_share", "competitor_anchor_usd_month", "fx")  # fmt: skip
    return [f"{k}: {_fmt(model[k])}" for k in keys if k in model]


def _trace(card: dict[str, Any], rule: str) -> dict[str, Any]:
    return next((e for e in card["rule_trace"] if e.get("rule") == rule), {})


def card_extra(card: dict[str, Any]) -> list[str]:
    lines = [
        f"category: {card['category']}",
        f"attractiveness: {card['attractiveness']}",
        f"confidence: {card['confidence']}",
        f"founder_fit: {card['founder_fit']}",
    ]
    lines += [f"{n}: level {f['level']}" for n, f in card["factors"].items()]
    conf = _trace(card, "confidence").get("inputs") or {}
    for k in ("strength", "hypothesis_share", "max_spread", "judges"):
        if k in conf:
            lines.append(f"confidence {k}: {_fmt(conf[k])}")
    fit = _trace(card, "founder_fit").get("inputs") or {}
    for k in ("mvp_feasible", "sales_motion", "hard_barriers", "mvp_months", "price_ceiling_high"):
        if k in fit:
            lines.append(f"founder_fit {k}: {_fmt(fit[k])}")
    return lines


def experiment_extra(card: dict[str, Any]) -> list[str]:
    e = card.get("experiment") or {}
    return [f"experiment {k}: {e[k]}" for k in ("name", "pass_fail", "cost_usd", "duration_days",
                                                "factor") if e.get(k) is not None]  # fmt: skip


def section_table(
    key: str, o: OppData, cfg: ReportDefaults, founder: dict[str, Any]
) -> tuple[list[Row], list[str]]:
    """Rows (at most ``max_claims_per_section``) and extra allowed sources of one section."""
    assert o.card is not None
    pool, card = o.pool, o.card

    def by(*abouts: str) -> list[Row]:
        return [r for r in pool if r.about in abouts]

    role_of = _roles_of(o)
    user = [r for r in pool if "user" in role_of.get(r.id, [])]
    barrier_ids = (_trace(card, "founder_fit").get("inputs") or {}).get("barrier_fact_ids") or []
    barriers = [r for r in pool if r.id in barrier_ids]
    experiment = card.get("experiment") or {}
    extra = [f"segment: {o.segment}"]
    groups: list[list[Row]]
    if key == "problem":
        groups = [by(PROBLEM), by(SIGNAL)[:4]]
    elif key == "evidence":
        groups = [by(PROBLEM), by(SIGNAL)]
    elif key == "who_has_it":
        groups = [by(PROBLEM), [r for r in by(SIGNAL) if r.actor], user]
    elif key == "current_solutions":
        groups = [by(GAP_EVIDENCE), by(SEGMENT), by(PRICE)]
    elif key == "gaps":
        groups = [by(GAP), by(GAP_EVIDENCE)]
    elif key == "buyers":
        groups = [by(ROLE), by(CHANNEL)]
        extra += [f"role: {r['role']}" for n in (*ROLES, "budget_owner")
                  if (r := o.buyer_roles.get(n))]  # fmt: skip
    elif key == "economics":
        groups = [by(ASSUMPTION), by(PRICE)]
        extra += economic_extra(o.economic_model)
    elif key == "risks":
        low = {i for f in card["factors"].values() if f["level"] <= 2 or f.get("capped")
               for i in f.get("claim_ids") or []}  # fmt: skip
        wtp = card["factors"].get("willingness_to_pay", {}).get("level", 5) <= 2
        groups = [
            [r for r in pool if r.id in low],
            [r for r in pool if r.kind == "hypothesis"],
            [r for r in pool if r.kind == "assumption" and r.sourced is False],
            barriers,
            by(PRICE) if wtp else [],
        ]
        extra += card_extra(card)
    elif key in ("proposed_product", "mvp"):
        groups = [by(PROBLEM), by(GAP), by(SIGNAL)[:3], user, barriers if key == "mvp" else []]
        extra += [f"solution_angle: {o.solution_angle}"]
        extra += [
            f"price_ceiling_usd_month: {_fmt(o.economic_model.get('price_ceiling_usd_month'))}"
        ]
        if key == "mvp":
            extra += [f"founder {k}: {v}" for k, v in founder.items()
                      if k in ("team", "mvp_months", "preferred_tech")]  # fmt: skip
            extra += card_extra(card)
    elif key == "validation_experiment":
        cited = set(card["factors"].get(experiment.get("factor"), {}).get("claim_ids") or [])
        groups = [[r for r in pool if r.id == experiment.get("claim_id")],
                  [r for r in pool if r.id in cited]]  # fmt: skip
        extra += experiment_extra(card)
    else:
        raise ValueError(f"unknown section {key!r}")
    return _balanced(groups, cfg.max_claims_per_section), extra


# --- claim views ----------------------------------------------------------------------------


def _clip(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def claim_views(
    session: Session, run_id: int, ids: set[int], max_chars: int
) -> dict[int, ClaimView]:
    """Views of claims with their excerpts (quote and translation clipped to ``max_chars``: never
    page text) and the source URL."""
    claims = session.scalars(select(Claim).where(Claim.run_id == run_id, Claim.id.in_(ids))).all()
    excerpt_ids = {e for c in claims for e in c.supports}
    rows = session.execute(
        select(Excerpt, Document)
        .join(Document, Excerpt.document_id == Document.id)
        .where(Excerpt.id.in_(excerpt_ids))
    ).all()
    excerpts = {
        e.id: ExcerptView(
            quote=_clip(e.quote, max_chars) or "",
            translation=_clip(e.translation, max_chars),
            url=d.url,
            domain=d.domain,
            date=d.published_at.date().isoformat() if d.published_at else None,
        )
        for e, d in rows
    }
    return {
        c.id: ClaimView(
            id=c.id, kind=c.kind, statement=c.statement, entailment=c.entailment,
            derived_from=list(c.derived_from or []),
            excerpts=[excerpts[e] for e in c.supports[:3] if e in excerpts],
            meta={k: v for k, v in (c.meta or {}).items() if k not in ("entailment_by",)},
        )
        for c in claims
    }  # fmt: skip


def row_payload(
    n: int, row: Row, views: dict[int, ClaimView], role_of: dict[int, list[str]]
) -> dict:
    """One numbered claim for the prompt."""
    view = views[row.id]
    item: dict[str, Any] = {"n": n, "kind": row.kind, "about": row.about}
    if row.about == SIGNAL:
        item |= {"signal_type": row.signal_type, "actor": row.actor}
    if row.competitor:
        item["competitor"] = row.competitor
    if row.id in role_of:
        item["roles"] = role_of[row.id]
    if row.entailment:
        item["entailment"] = row.entailment
    if row.sourced is not None:
        item["sourced"] = row.sourced
    item["statement"] = claim_text(view.model_copy(update={"excerpts": []}))
    if row.kind == "fact" and view.excerpts and view.excerpts[0].translation:
        item["translation"] = _clip(view.excerpts[0].translation, 300)
    return item


def tables_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1)
