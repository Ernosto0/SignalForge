"""The buyer-roles exit check (agent-modules.md §6, part of M5): every buyer role of every
opportunity cites claims of the run that have not failed entailment, or a stored hypothesis claim,
and a role that failed the role check no longer cites facts.

The ``buyers`` stage enforces this when it writes; this check re-reads the stored rows, so a later
edit or bug cannot slip through. A run without opportunities fails, so a run where buyers never
ran does not pass vacuously.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from signalforge.db.models import Claim, Opportunity
from signalforge.evidence.entailment import failed

REQUIRED_ROLES = ("user", "buyer", "decision_maker", "economic_beneficiary")


def role_problems(role: dict[str, Any], claims: dict[int, Claim]) -> list[str]:
    """Why a stored role breaks the rule (empty if it holds)."""
    errors = []
    for claim_id in role.get("claim_ids") or []:
        claim = claims.get(claim_id)
        if claim is None:
            errors.append(f"cites claim {claim_id}, which is not in this run")
        elif failed(claim.entailment):
            errors.append(f"cites claim {claim_id}, which failed entailment ({claim.entailment})")
    if failed(role.get("entailment")) and any(
        claims[i].kind == "fact" for i in role.get("claim_ids") or [] if i in claims
    ):
        errors.append("still cites facts that failed the role check")
    hypothesis_id = role.get("hypothesis_claim_id")
    if hypothesis_id is not None:
        claim = claims.get(hypothesis_id)
        if claim is None or claim.kind != "hypothesis":
            errors.append(f"hypothesis claim {hypothesis_id} is missing or not a hypothesis")
    elif role.get("hypothesis"):
        errors.append("hypothesis not stored as a claim")
    elif not role.get("claim_ids"):
        errors.append("neither a citation nor a hypothesis")
    return errors


def check_buyer_roles(session: Session, run_id: int) -> list[str]:
    """Violations of the buyer-roles rule in a run's stored opportunities (empty = it holds)."""
    opportunities = session.scalars(
        select(Opportunity).where(Opportunity.run_id == run_id).order_by(Opportunity.id)
    ).all()
    if not opportunities:
        return [f"run {run_id} has no opportunities; run buyers first"]
    claims = {c.id: c for c in session.scalars(select(Claim).where(Claim.run_id == run_id))}
    errors = []
    for o in opportunities:
        roles = o.buyer_roles or {}
        for name in (*REQUIRED_ROLES, "budget_owner"):
            role = roles.get(name)
            where = f"opportunity {o.id} ({o.segment}), {name}"
            if role is None:
                if name != "budget_owner":  # a null budget owner is Gate 2's to judge
                    errors.append(f"{where}: missing")
                continue
            errors += [f"{where}: {p}" for p in role_problems(role, claims)]
        for claim_id in roles.get("gap_claim_ids") or []:
            if claim_id not in claims:
                errors.append(f"opportunity {o.id}: gap claim {claim_id} is not in this run")
    return errors
