"""The report sections written in code, with no LLM (agent-modules.md §9): they are built from rows
and rule traces, so they cannot contradict them. ``check_report`` rebuilds the first three."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from signalforge.config import ScoreDefaults
from signalforge.db.models import (
    Claim,
    Competitor,
    LLMCall,
    Opportunity,
    ProblemCluster,
    ResearchRun,
    ScoreCard,
    Signal,
    StageRun,
)
from signalforge.packs import MarketPack
from signalforge.reporting.schema import (
    DontBuildItem,
    InsufficientItem,
    MethodView,
    OpportunitySummary,
    QuoteView,
    RankingRow,
    Reason,
)
from signalforge.reporting.tables import OppData, _clip

KNOCKOUT_TEXT = {
    "no_budget_owner": "no budget owner was found",
    "value_below_minimum": "the price ceiling is below the founder's minimum customer value",
}
INSUFFICIENT_QUOTES = 2  # verified quotes shown per insufficient-evidence cluster
CATEGORY_TEXT = {
    "weak": "severity is at or below the weak threshold",
    "false_positive": "too few supported signal facts",
}


def _fired_category(card: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (e for e in card["rule_trace"] if str(e.get("rule", "")).startswith("category.")
         and e.get("fired")),
        None,
    )  # fmt: skip


def other_opportunities(
    opps: list[OppData], full_ids: set[int], cfg: ScoreDefaults
) -> list[OpportunitySummary]:
    """Scored opportunities that did not get a full report."""
    out = []
    for o in opps:
        if o.card is None or o.card["attractiveness"] is None or o.id in full_ids:
            continue
        card = o.card
        fired = _fired_category(card)
        top = max(
            card["factors"],
            key=lambda n: cfg.weights.get(n, 0) * card["factors"][n]["level"],
            default=None,
        )
        out.append(
            OpportunitySummary(
                opportunity_id=o.id, segment=o.segment, category=card["category"],
                attractiveness=card["attractiveness"], confidence=card["confidence"],
                founder_fit=card["founder_fit"], fired_rule=fired["rule"] if fired else None,
                top_factor=top,
            )
        )  # fmt: skip
    return out


def dont_build(opps: list[OppData]) -> list[DontBuildItem]:
    """Knocked-out, weak and false-positive opportunities with the rules that decided it."""
    out = []
    for o in opps:
        card = o.card
        if card is None:
            continue
        reasons: list[Reason] = []
        if o.status == "knocked_out":
            reasons = [
                Reason(rule=f"gate2.{k['rule']}", text=KNOCKOUT_TEXT.get(k["rule"], k["rule"]),
                       detail=k.get("detail"))
                for k in o.knockouts
            ]  # fmt: skip
        elif card["category"] in CATEGORY_TEXT:
            fired = _fired_category(card)
            inputs = (fired or {}).get("inputs") or {}
            reasons = [
                Reason(rule=(fired or {}).get("rule", f"category.{card['category']}"),
                       text=CATEGORY_TEXT[card["category"]],
                       detail=", ".join(f"{k} {v}" for k, v in inputs.items()) or None)
            ]  # fmt: skip
        if reasons:
            out.append(
                DontBuildItem(opportunity_id=o.id, segment=o.segment, category=card["category"],
                              reasons=reasons)
            )  # fmt: skip
    return out


def insufficient_evidence(
    landscape: dict[str, Any], opportunity_problem_ids: set[int], max_chars: int
) -> list[InsufficientItem]:
    """Clusters that failed Gate 1 or verify, and shortlisted ones that produced no opportunity.
    ``landscape`` is ``build_landscape`` output (verified quotes only)."""
    out = []
    rows = [(p, None) for p in landscape["insufficient_evidence"]]
    rows += [
        (p, "no opportunity was produced for this shortlisted problem")
        for p in landscape["shortlist"]
        if p["id"] not in opportunity_problem_ids
    ]
    for p, reason in rows:
        v = p.get("verification")
        if reason:
            why = reason
        elif v and v.get("passed") is False:
            why = "failed verification"
        else:
            why = "failed Gate 1"
        out.append(
            InsufficientItem(
                cluster_id=p["id"], name=p["name"], why=why, failed_rules=p["failed_rules"],
                signals=p["signals"], independent_sources=p["independent_sources"],
                strength=p["evidence_strength"],
                quotes=[
                    QuoteView(
                        quote=_clip(q["quote"], max_chars) or "",
                        translation=_clip(q["translation"], max_chars), url=q["url"],
                        domain=q["domain"], date=q["published"], statement=q["statement"],
                    )
                    for q in p["quotes"][:INSUFFICIENT_QUOTES]
                ],
            )
        )  # fmt: skip
    return out


def summary_ranking(full: list[OppData]) -> list[RankingRow]:
    """The full reports in report order with their card's category and scores."""
    return [
        RankingRow(
            opportunity_id=o.id, title=o.segment, category=(o.card or {})["category"],
            attractiveness=(o.card or {})["attractiveness"],
            confidence=(o.card or {})["confidence"], founder_fit=(o.card or {})["founder_fit"],
        )
        for o in full
    ]  # fmt: skip


def build_method(session: Session, run_id: int, pack: MarketPack) -> MethodView:
    """Counts, cost per stage, models and prompt versions of the run so far. The ``report``
    stage's own StageRun row does not exist yet while it runs."""
    run = session.get(ResearchRun, run_id)
    assert run is not None

    def count(model: Any, *where: Any) -> int:
        return session.scalar(select(func.count()).select_from(model).where(*where)) or 0

    stage_costs: dict[str, float] = {}
    for s in session.scalars(
        select(StageRun)
        .where(
            StageRun.run_id == run_id, StageRun.status == "completed", StageRun.stage != "report"
        )  # a previous report run is not this one
        .order_by(StageRun.id)
    ):
        stage_costs[s.stage] = float(s.cost_usd)  # the latest StageRun per stage wins
    calls = session.execute(
        select(LLMCall.stage, LLMCall.model, LLMCall.prompt_id, LLMCall.prompt_version)
        .where(LLMCall.run_id == run_id)
        .distinct()
        .order_by(LLMCall.stage, LLMCall.model, LLMCall.prompt_id, LLMCall.prompt_version)
    ).all()
    models: dict[str, list[str]] = {}
    for stage, model, _, _ in calls:
        models.setdefault(stage or "-", [])
        if model not in models[stage or "-"]:
            models[stage or "-"].append(model)
    return MethodView(
        pack=f"{pack.id}@{pack.version}",
        run_pack=f"{run.pack_id}@{run.pack_version}",
        counts={
            "signals": count(Signal, Signal.run_id == run_id),
            "claims": count(Claim, Claim.run_id == run_id),
            "clusters": count(ProblemCluster, ProblemCluster.run_id == run_id),
            "shortlisted": count(
                ProblemCluster,
                ProblemCluster.run_id == run_id,
                ProblemCluster.shortlisted.is_(True),
            ),  # fmt: skip
            "competitors": count(Competitor, Competitor.run_id == run_id),
            "opportunities": count(Opportunity, Opportunity.run_id == run_id),
            "score_cards": count(
                ScoreCard,
                ScoreCard.opportunity_id.in_(
                    select(Opportunity.id).where(Opportunity.run_id == run_id)
                ),
            ),  # fmt: skip
        },
        stage_costs=stage_costs,
        models=models,
        prompts=sorted({f"{p}@{v}" for _, _, p, v in calls}),
    )
