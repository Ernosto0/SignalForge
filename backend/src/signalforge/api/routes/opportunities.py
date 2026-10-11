from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from signalforge.api.deps import DB
from signalforge.api.schemas import (
    ClaimOut,
    CompetitorRef,
    ExcerptRef,
    GapMatrixOut,
    OpportunityCard,
    OpportunityDetail,
    ProblemOut,
    ScoreOut,
    ValueRange,
)
from signalforge.db.models import (
    Claim,
    Competitor,
    Document,
    Excerpt,
    GapMatrix,
    Opportunity,
    ProblemCluster,
    ResearchRun,
    ScoreCard,
)

router = APIRouter(tags=["opportunities"])


@router.get("/runs/{run_id}/opportunities", response_model=list[OpportunityCard])
def list_opportunities(run_id: int, db: DB) -> list[OpportunityCard]:
    """By attractiveness, highest first; knocked-out (unscored) opportunities last."""
    with db() as session:
        if session.get(ResearchRun, run_id) is None:
            raise HTTPException(404, f"run {run_id} not found")
        rows = session.execute(
            _cards()
            .where(Opportunity.run_id == run_id)
            .order_by(ScoreCard.attractiveness.desc().nulls_last(), Opportunity.id)
        ).all()
    return [_card(row) for row in rows]


@router.get("/opportunities/{opportunity_id}", response_model=OpportunityDetail)
def get_opportunity(opportunity_id: int, db: DB) -> OpportunityDetail:
    with db() as session:
        row = session.execute(_cards().where(Opportunity.id == opportunity_id)).one_or_none()
        if row is None:
            raise HTTPException(404, f"opportunity {opportunity_id} not found")
        opp, card, problem = row
        matrix = session.scalar(select(GapMatrix).where(GapMatrix.problem_id == problem.id))
        detail = OpportunityDetail(
            **_card(row).model_dump(),
            problem=ProblemOut.model_validate(problem, from_attributes=True),
            buyer_roles=opp.buyer_roles or {},
            accessibility=opp.accessibility or {},
            market_breadth=opp.market_breadth or {},
            economic_model=opp.economic_model or {},
            wtp_signals=opp.wtp_signals or [],
            score=ScoreOut.model_validate(card, from_attributes=True) if card else None,
            gap_matrix=_gap_matrix(session, matrix.matrix) if matrix else None,
            claims={},
        )
        detail.claims = _claims(session, opp.run_id, _claim_ids(detail.model_dump()))
    return detail


def _cards() -> Any:
    return (
        select(Opportunity, ScoreCard, ProblemCluster)
        .join(ProblemCluster, ProblemCluster.id == Opportunity.problem_id)
        .outerjoin(ScoreCard, ScoreCard.opportunity_id == Opportunity.id)
    )


def _card(row: Row[tuple[Opportunity, ScoreCard | None, ProblemCluster]]) -> OpportunityCard:
    opp, card, problem = row
    return OpportunityCard(
        id=opp.id,
        run_id=opp.run_id,
        problem_id=problem.id,
        problem_name=problem.name,
        segment=opp.segment,
        solution_angle=opp.solution_angle,
        gate2_status=opp.status,
        knockouts=opp.knockouts or [],
        category=card.category if card else None,
        attractiveness=card.attractiveness if card else None,
        confidence=card.confidence if card else None,
        founder_fit=card.founder_fit if card else None,
        evidence_strength=problem.evidence_strength,
        value_usd_month=_value_range(opp.economic_model),
    )


def _value_range(model: dict[str, Any] | None) -> ValueRange | None:
    """``economic_model.value_usd_month`` is ``[low, high]`` (monetization stage)."""
    value = (model or {}).get("value_usd_month")
    if not isinstance(value, list | tuple) or len(value) != 2:
        return None
    low, high = value
    if not all(isinstance(v, int | float) for v in (low, high)):
        return None
    return ValueRange(low=low, high=high)


def _gap_matrix(session: Session, matrix: dict[str, Any]) -> GapMatrixOut:
    ids = [int(i) for i in matrix.get("competitor_ids", [])]
    by_id = {c.id: c for c in session.scalars(select(Competitor).where(Competitor.id.in_(ids)))}
    return GapMatrixOut(
        dimensions=matrix.get("dimensions", []),
        competitors=[
            CompetitorRef.model_validate(by_id[i], from_attributes=True) for i in ids if i in by_id
        ],
        # JSONB object keys are strings already: {dimension: {"<competitor id>": cell}}.
        cells=matrix.get("cells", {}),
        gaps=matrix.get("gaps", []),
    )


def _claim_ids(payload: Any) -> set[int]:
    """Claim ids under ``claim_id``, ``claim_ids``, ``*_claim_id`` and ``*_claim_ids`` keys."""
    found: set[int] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "claim_id" or key.endswith("_claim_id"):
                if isinstance(value, int):
                    found.add(value)
            elif key == "claim_ids" or key.endswith("_claim_ids"):
                if isinstance(value, list):
                    found.update(v for v in value if isinstance(v, int))
            else:
                found |= _claim_ids(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _claim_ids(item)
    return found


def _claims(session: Session, run_id: int, ids: set[int]) -> dict[str, ClaimOut]:
    """The referenced claims plus one level of the claims they derive from, with their excerpts."""
    claims = _load_claims(session, run_id, ids)
    derived = {d for c in claims for d in c.derived_from} - ids
    claims += _load_claims(session, run_id, derived)
    excerpt_ids = {e for c in claims for e in c.supports}
    excerpts = {
        excerpt.id: ExcerptRef(
            id=excerpt.id,
            quote=excerpt.quote,
            translation=excerpt.translation,
            verified=excerpt.verified,
            url=doc.url,
            domain=doc.domain,
            source_category=doc.source_category,
            quality_tier=doc.quality_tier,
        )
        for excerpt, doc in session.execute(
            select(Excerpt, Document)
            .join(Document, Document.id == Excerpt.document_id)
            .where(Excerpt.id.in_(excerpt_ids))
        )
    }
    return {
        str(c.id): ClaimOut(
            id=c.id,
            kind=c.kind,
            statement=c.statement,
            entailment=c.entailment,
            derived_from=list(c.derived_from or []),
            excerpts=[excerpts[e] for e in c.supports or [] if e in excerpts],
        )
        for c in sorted(claims, key=lambda c: c.id)
    }


def _load_claims(session: Session, run_id: int, ids: set[int]) -> list[Claim]:
    if not ids:
        return []
    return list(session.scalars(select(Claim).where(Claim.run_id == run_id, Claim.id.in_(ids))))
