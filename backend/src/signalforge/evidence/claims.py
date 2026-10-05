"""Typed claims (plan §7.2): the only things a report may cite.

- ``fact``: backed by ≥1 verified excerpt (``supports``).
- ``inference``: derived from ≥1 other claim (``derived_from``), so its chain can be shown.
- ``hypothesis``: may have no support; always rendered as a hypothesis.

``assumption`` claims carry dated numeric values and arrive with ``Claim.meta`` in M5. Every helper
adds to the session and flushes, so the returned claim has its id. Stages own their claims by
``stage`` and replace them with :func:`delete_stage_claims`.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from signalforge.db.models import Claim


@dataclass(frozen=True)
class ClaimRow:
    id: int
    kind: str
    statement: str
    supports: list[int]
    derived_from: list[int]
    stage: str


def _add(session: Session, claim: Claim) -> Claim:
    session.add(claim)
    session.flush()
    return claim


def add_fact(
    session: Session, run_id: int, statement: str, excerpt_ids: Iterable[int], *, stage: str
) -> Claim:
    supports = sorted(set(excerpt_ids))
    if not supports:
        raise ValueError("a fact claim needs at least one verified excerpt")
    return _add(
        session,
        Claim(run_id=run_id, kind="fact", statement=statement, supports=supports, stage=stage),
    )


def add_inference(
    session: Session, run_id: int, statement: str, claim_ids: Iterable[int], *, stage: str
) -> Claim:
    derived = sorted(set(claim_ids))
    if not derived:
        raise ValueError("an inference claim needs at least one claim it is derived from")
    return _add(
        session,
        Claim(
            run_id=run_id, kind="inference", statement=statement, derived_from=derived, stage=stage
        ),
    )


def add_hypothesis(
    session: Session,
    run_id: int,
    statement: str,
    *,
    stage: str,
    excerpt_ids: Iterable[int] = (),
    claim_ids: Iterable[int] = (),
) -> Claim:
    return _add(
        session,
        Claim(
            run_id=run_id,
            kind="hypothesis",
            statement=statement,
            supports=sorted(set(excerpt_ids)),
            derived_from=sorted(set(claim_ids)),
            stage=stage,
        ),
    )


def delete_stage_claims(session: Session, run_id: int, stage: str) -> None:
    """Remove the claims a stage wrote for a run (stages are idempotent, agent-modules §0.3)."""
    session.execute(delete(Claim).where(Claim.run_id == run_id, Claim.stage == stage))


def fact_ids_by_excerpt(session: Session, run_id: int, stage: str) -> dict[int, int]:
    """Excerpt id -> id of the fact claim it supports, for single-excerpt facts of ``stage``."""
    rows = session.execute(
        select(Claim.id, Claim.supports).where(
            Claim.run_id == run_id, Claim.stage == stage, Claim.kind == "fact"
        )
    )
    return {supports[0]: claim_id for claim_id, supports in rows if len(supports) == 1}


def claim_table(session: Session, run_id: int, ids: Iterable[int]) -> list[ClaimRow]:
    """The given claims of a run, in id order; ids from other runs are left out."""
    wanted = sorted(set(ids))
    if not wanted:
        return []
    rows = session.scalars(
        select(Claim).where(Claim.run_id == run_id, Claim.id.in_(wanted)).order_by(Claim.id)
    )
    return [
        ClaimRow(c.id, c.kind, c.statement, list(c.supports), list(c.derived_from), c.stage)
        for c in rows
    ]
