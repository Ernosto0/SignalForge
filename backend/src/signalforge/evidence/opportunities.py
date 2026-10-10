"""The M5 exit checks: buyer roles (agent-modules.md §6), economic models (§7) and score cards
(§8). Buyer roles: every buyer role of every opportunity cites claims of the run that have not
failed entailment, or a stored hypothesis claim, and a role that failed the role check no longer
cites facts.

The ``buyers`` stage enforces this when it writes; this check re-reads the stored rows, so a later
edit or bug cannot slip through. A run without opportunities fails, so a run where buyers never
ran does not pass vacuously.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from signalforge.config import ScoreDefaults, get_defaults
from signalforge.db.models import Claim, Opportunity, ScoreCard
from signalforge.evidence.entailment import failed
from signalforge.scoring.categories import ORDER as CATEGORY_ORDER
from signalforge.scoring.economics import FORMULAS
from signalforge.scoring.scorer import CAP_RULES, CODE_RULES, attractiveness

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


def check_economic_models(session: Session, run_id: int) -> list[str]:
    """Violations of the monetization rule (agent-modules.md §7, part of M5): every opportunity
    was judged by Gate 2, and every number in a valid economic model traces to an assumption claim
    of the run that has a source URL (a pack reference) or is labelled unsourced."""
    opportunities = session.scalars(
        select(Opportunity).where(Opportunity.run_id == run_id).order_by(Opportunity.id)
    ).all()
    if not opportunities:
        return [f"run {run_id} has no opportunities; run buyers first"]
    claims = {c.id: c for c in session.scalars(select(Claim).where(Claim.run_id == run_id))}
    errors = []
    for o in opportunities:
        where = f"opportunity {o.id} ({o.segment})"
        if o.status not in ("passed", "knocked_out"):
            errors.append(f"{where}: not judged by Gate 2 (status {o.status!r})")
        model = o.economic_model or {}
        if model.get("status") != "ok":
            continue
        inputs = model.get("inputs") or {}
        expected = {i.name for i in FORMULAS.get(model.get("formula", ""), ())}
        if not expected or set(inputs) != expected:
            errors.append(f"{where}: inputs {sorted(inputs)} do not match {model.get('formula')}")
        for name, claim_id in inputs.items():
            claim = claims.get(claim_id)
            if claim is None or claim.kind != "assumption":
                errors.append(f"{where}, {name}: claim {claim_id} is not an assumption of the run")
                continue
            meta = claim.meta or {}
            if not isinstance(meta.get("sourced"), bool):
                errors.append(f"{where}, {name}: no sourced/unsourced label")
            elif meta["sourced"] and not meta.get("source_url"):
                errors.append(f"{where}, {name}: labelled sourced without a source")
            if meta.get("currency") and not meta.get("as_of"):
                errors.append(f"{where}, {name}: money value without a date")
        fx = model.get("fx") or {}
        if not (fx.get("rate") and fx.get("as_of") and fx.get("source_url")):
            errors.append(f"{where}: exchange rate without a date or source")
    return errors


def check_score_cards(session: Session, run_id: int, cfg: ScoreDefaults | None = None) -> list[str]:
    """Violations of the M5 exit (agent-modules.md §8): every opportunity has a ScoreCard that is
    fully explainable from its rule trace and cited claims.

    - A knocked-out card is ``weak``, has no factors, null attractiveness / confidence / founder
      fit, and traces each knock-out.
    - A passed card has all seven factors at levels 1–5. Each cites existing claims of the run
      that have not failed entailment, or has a code-rule trace entry; a capped factor has its
      cap entry; a model level above the uncited cap cites a fact. Attractiveness recomputes from
      the levels; every category rule is traced and the stored category is the first that fired;
      confidence and founder fit trace their inputs and result; the experiment cites a claim the
      factors cite.
    """
    cfg = cfg or get_defaults().score
    opportunities = session.scalars(
        select(Opportunity).where(Opportunity.run_id == run_id).order_by(Opportunity.id)
    ).all()
    if not opportunities:
        return [f"run {run_id} has no opportunities; run buyers first"]
    cards = {
        c.opportunity_id: c
        for c in session.scalars(
            select(ScoreCard).where(ScoreCard.opportunity_id.in_([o.id for o in opportunities]))
        )
    }
    claims = {c.id: c for c in session.scalars(select(Claim).where(Claim.run_id == run_id))}
    errors = []
    for o in opportunities:
        where = f"opportunity {o.id} ({o.segment})"
        card = cards.get(o.id)
        if card is None:
            errors.append(f"{where}: no score card")
            continue
        if o.status == "knocked_out":
            errors += [f"{where}: {e}" for e in _knocked_out_card_problems(o, card)]
        elif o.status == "passed":
            errors += [f"{where}: {e}" for e in _card_problems(card, claims, cfg)]
        else:
            errors.append(f"{where}: not judged by Gate 2 (status {o.status!r})")
    return errors


def _knocked_out_card_problems(o: Opportunity, card: ScoreCard) -> list[str]:
    errors = []
    if card.category != "weak" or card.factors:
        errors.append("knocked out but not a weak card without factors")
    if (card.attractiveness, card.confidence, card.founder_fit) != (None, None, None):
        errors.append("knocked out but has attractiveness, confidence or founder fit")
    traced = {e.get("rule") for e in card.rule_trace}
    for k in o.knockouts:
        if f"gate2.{k['rule']}" not in traced:
            errors.append(f"knock-out {k['rule']} not in the rule trace")
    return errors


def _card_problems(card: ScoreCard, claims: dict[int, Claim], cfg: ScoreDefaults) -> list[str]:
    errors = []
    if card.attractiveness is None or card.confidence is None or card.founder_fit is None:
        errors.append("passed but attractiveness, confidence or founder fit is null")
    trace = card.rule_trace or []
    factors = card.factors or {}
    if set(factors) != set(cfg.weights):
        return errors + [f"factors {sorted(factors)} are not {sorted(cfg.weights)}"]

    def traced(rules: frozenset[str], factor: str) -> bool:
        return any(e.get("rule") in rules and e.get("factor") == factor for e in trace)

    for name, f in factors.items():
        level, ids = f.get("level"), f.get("claim_ids") or []
        if not isinstance(level, int) or not 1 <= level <= 5:
            errors.append(f"{name}: level {level!r} is not 1–5")
            continue
        bad = [i for i in ids if i not in claims or failed(claims[i].entailment)]
        if bad:
            errors.append(f"{name}: cites {bad}, missing from the run or failed entailment")
        valid = [i for i in ids if i not in bad]
        if not valid and not traced(CODE_RULES, name):
            errors.append(f"{name}: neither a cited claim nor a code-rule trace entry")
        if f.get("capped") and not traced(CAP_RULES, name):
            errors.append(f"{name}: capped without a cap entry in the trace")
        if (
            f.get("source") == "model"
            and level > cfg.uncited_cap
            and not any(claims[i].kind == "fact" for i in valid)
        ):
            errors.append(f"{name}: level {level} above the uncited cap without a cited fact")
    if all(isinstance(f.get("level"), int) for f in factors.values()):
        expected = attractiveness({n: f["level"] for n, f in factors.items()}, cfg.weights)
        if card.attractiveness is None or abs(card.attractiveness - expected) > 0.01:
            errors.append(f"attractiveness {card.attractiveness} is not {expected}")
    rules = [e for e in trace if str(e.get("rule", "")).startswith("category.")]
    if [e["rule"] for e in rules] != [f"category.{n}" for n in CATEGORY_ORDER]:
        errors.append("category rules missing from the trace")
    else:
        first = next((e["rule"] for e in rules if e.get("fired")), None)
        if first != f"category.{card.category}":
            errors.append(f"category {card.category!r} is not the first fired rule ({first})")
    for rule, value in (("confidence", card.confidence), ("founder_fit", card.founder_fit)):
        entry = next((e for e in trace if e.get("rule") == rule), None)
        if entry is None or not entry.get("inputs"):
            errors.append(f"{rule} without a trace entry listing its inputs")
        elif entry.get("result") != value:
            errors.append(f"{rule} {value!r} does not match its trace ({entry.get('result')!r})")
    experiment = card.experiment or {}
    cited = {i for f in factors.values() for i in f.get("claim_ids") or []}
    if experiment.get("claim_id") not in claims or experiment.get("claim_id") not in cited:
        errors.append(f"experiment claim {experiment.get('claim_id')} is not a cited claim")
    return errors
