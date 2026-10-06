"""The M4 exit check: every gap-matrix cell is a cited fact or ``unknown`` (plan §13).

A cell with a value (``yes`` / ``partial`` / ``no``) must cite a fact claim of the same run that
belongs to that cell's competitor, rests on stored excerpts, and has not failed entailment. Every
(dimension, competitor) pair must have a cell, and every shortlisted problem a matrix (the stage
writes one even when no competitor is confirmed), so a run where competitors never ran fails the
check instead of passing it vacuously. The ``competitors`` stage enforces the cell rule when it
writes a matrix; this check re-reads the stored rows, so a later edit or bug cannot slip through.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from signalforge.db.models import Claim, Competitor, Excerpt, GapMatrix, ProblemCluster
from signalforge.evidence.entailment import failed

UNKNOWN = "unknown"
VALUES = {"yes", "partial", "no", UNKNOWN}


def cell_problem(
    cell: dict[str, Any], competitor_id: int, claims: dict[int, Claim], excerpts: set[int]
) -> str | None:
    """Why a cell breaks the rule, or ``None`` if it holds."""
    value = cell.get("value")
    if value not in VALUES:
        return f"value {value!r} is not one of {sorted(VALUES)}"
    if value == UNKNOWN:
        return None
    claim_id = cell.get("claim_id")
    if claim_id is None:
        return f"{value!r} without a citation"
    claim = claims.get(claim_id)
    if claim is None:
        return f"cites claim {claim_id}, which is not in this run"
    if claim.kind != "fact":
        return f"cites claim {claim_id} of kind {claim.kind!r}, not a fact"
    if (claim.meta or {}).get("competitor_id") != competitor_id:
        return f"cites claim {claim_id} of another competitor"
    if failed(claim.entailment):
        return f"cites claim {claim_id}, which failed entailment ({claim.entailment})"
    if not claim.supports or not set(claim.supports) <= excerpts:
        return f"cites claim {claim_id}, whose excerpts are missing"
    return None


def check_gap_matrices(session: Session, run_id: int) -> list[str]:
    """Violations of the M4 exit rule in a run's stored gap matrices (empty = the rule holds)."""
    matrices = session.scalars(select(GapMatrix).where(GapMatrix.run_id == run_id)).all()
    competitors = {
        c.id: c.problem_id
        for c in session.scalars(select(Competitor).where(Competitor.run_id == run_id))
    }
    claims = {c.id: c for c in session.scalars(select(Claim).where(Claim.run_id == run_id))}
    excerpts = set(session.scalars(select(Excerpt.id).where(Excerpt.run_id == run_id)))
    shortlisted = set(
        session.scalars(
            select(ProblemCluster.id).where(
                ProblemCluster.run_id == run_id, ProblemCluster.shortlisted.is_(True)
            )
        )
    )
    if not matrices:
        return [f"run {run_id} has no gap matrices; run competitors first"]
    missing = sorted(shortlisted - {m.problem_id for m in matrices})
    errors = [f"problem {p}: shortlisted but has no gap matrix" for p in missing]
    for m in matrices:
        matrix = m.matrix or {}
        dims = [d["key"] for d in matrix.get("dimensions", [])]
        cells = matrix.get("cells", {})
        for competitor_id in matrix.get("competitor_ids", []):
            if competitors.get(competitor_id) != m.problem_id:
                errors.append(f"problem {m.problem_id}: competitor {competitor_id} is not its own")
            for dim in dims:
                cell = cells.get(dim, {}).get(str(competitor_id))
                where = f"problem {m.problem_id}, {dim} × competitor {competitor_id}"
                if cell is None:
                    errors.append(f"{where}: no cell")
                elif problem := cell_problem(cell, competitor_id, claims, excerpts):
                    errors.append(f"{where}: {problem}")
    return errors
