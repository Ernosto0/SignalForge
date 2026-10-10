"""Factor levels, attractiveness, confidence and founder fit (plan §8.2–8.4; agent-modules.md §8).

Pure functions over validated inputs; the ``score`` stage loads and writes. Every rule that
changes or sets a level leaves a rule-trace entry ``{rule, factor?, fired, inputs, …}`` so a card
is explainable from its trace and cited claims.

- Five factors are judged by the model on the rubric; ``economic_impact`` (value midpoint) and
  ``market_breadth`` (breadth not searched yet) are code.
- A model factor whose valid citations include no fact is capped at ``uncited_cap``;
  ``competition_gap`` is capped at ``no_gap_cap`` when the opportunity targets no gap.
- The WTP floor counts distinct competitors with a price convertible to USD/month whose price
  fact has not failed entailment, never price rows.
- Caps, floor and code factors apply per judge, before the median, so the median cannot escape
  them.
"""

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from signalforge.config import ScoreDefaults
from signalforge.domain.scoring import FeasibilityJudgment, RubricJudgments

MODEL_FACTORS = (
    "severity",
    "frequency",
    "competition_gap",
    "willingness_to_pay",
    "customer_accessibility",
)
CODE_FACTORS = ("economic_impact", "market_breadth")
# Trace rules that explain a factor level without a model citation.
CODE_RULES = frozenset({"factor.economic_impact", "factor.market_breadth", "wtp_floor",
                        "missing_factor"})  # fmt: skip
CAP_RULES = frozenset({"uncited_cap", "no_gap_cap"})
RUBRIC = Path(__file__).parent / "rubric.yaml"


@cache
def load_rubric() -> dict[str, Any]:
    """``{version, factors}``: the anchors for the model-judged factors."""
    rubric = yaml.safe_load(RUBRIC.read_text(encoding="utf-8"))
    if set(rubric["factors"]) != set(MODEL_FACTORS):
        raise ValueError(f"rubric.yaml must anchor exactly {MODEL_FACTORS}")
    return rubric


@dataclass(frozen=True)
class Cited:
    """A claim-table row as the scorer sees it."""

    id: int
    kind: str  # fact | inference | hypothesis | assumption
    entailment: str | None = None
    sourced: bool | None = None  # assumptions only


@dataclass
class FactorScore:
    level: int
    source: str  # model | code
    justification: str = ""
    claim_ids: list[int] = field(default_factory=list)
    capped: bool = False
    floor: bool = False
    missing: bool = False

    def as_dict(self, weight: int, judges: list[int], spread: int) -> dict[str, Any]:
        out: dict[str, Any] = {
            "level": self.level,
            "weight": weight,
            "source": self.source,
            "justification": self.justification,
            "claim_ids": self.claim_ids,
            "capped": self.capped,
            "judges": judges,
            "spread": spread,
        }
        return (
            out
            | ({"floor": True} if self.floor else {})
            | ({"missing": True} if self.missing else {})
        )


@dataclass(frozen=True)
class CodeFactors:
    """What code decides for one opportunity, shared by all its judges."""

    economic_impact: tuple[int, list[int]]  # level, assumption claim ids
    market_breadth: int
    has_gaps: bool
    floor_competitors: dict[str, list[int]]  # competitor -> qualifying price claim ids
    trace: list[dict[str, Any]]  # factor.economic_impact, factor.market_breadth, wtp_floor


@dataclass
class Judge:
    """One validated rubric judgment over all seven factors."""

    factors: dict[str, FactorScore]
    events: list[dict[str, Any]] = field(default_factory=list)  # caps, missing factors
    notes: Counter[str] = field(default_factory=Counter)


# --- code factors ---------------------------------------------------------------------------


def economic_impact(
    model: Mapping[str, Any], cfg: ScoreDefaults
) -> tuple[int, list[int], dict[str, Any]]:
    """Level from the midpoint of ``value_usd_month``; 1 when the model is not valid."""
    status = model.get("status")
    ids = list((model.get("inputs") or {}).values())
    inputs: dict[str, Any] = {"status": status, "boundaries": cfg.economic_impact_levels_usd_month}
    if status != "ok":
        entry = {"rule": "factor.economic_impact", "factor": "economic_impact", "fired": True,
                 "inputs": inputs, "level": 1, "note": "no valid economic model"}  # fmt: skip
        return 1, ids, entry
    low, high = model["value_usd_month"]
    mid = round((low + high) / 2, 2)
    level = 1 + sum(b <= mid for b in cfg.economic_impact_levels_usd_month)
    inputs |= {"value_usd_month": [low, high], "midpoint": mid}
    entry = {"rule": "factor.economic_impact", "factor": "economic_impact", "fired": True,
             "inputs": inputs, "level": level}  # fmt: skip
    return level, ids, entry


def market_breadth(breadth: Mapping[str, Any], cfg: ScoreDefaults) -> tuple[int, dict[str, Any]]:
    """Breadth search is deferred: a fixed level while no breadth facts exist."""
    level = cfg.market_breadth_unsearched_level
    entry = {"rule": "factor.market_breadth", "factor": "market_breadth", "fired": True,
             "inputs": {"status": breadth.get("status"), "hint": breadth.get("hint")},
             "level": level, "note": "breadth not searched; no breadth facts"}  # fmt: skip
    return level, entry


def wtp_floor_entry(competitors: Mapping[str, list[int]], cfg: ScoreDefaults) -> dict[str, Any]:
    f = cfg.wtp_floor
    return {"rule": "wtp_floor", "factor": "willingness_to_pay",
            "fired": len(competitors) >= f.min_competitors,
            "inputs": {"competitors": dict(competitors), "min_competitors": f.min_competitors,
                       "level": f.level}}  # fmt: skip


def code_factors(
    model: Mapping[str, Any],
    breadth: Mapping[str, Any],
    has_gaps: bool,
    floor_competitors: Mapping[str, list[int]],
    cfg: ScoreDefaults,
) -> CodeFactors:
    econ_level, econ_ids, econ_entry = economic_impact(model, cfg)
    breadth_level, breadth_entry = market_breadth(breadth, cfg)
    return CodeFactors(
        (econ_level, econ_ids),
        breadth_level,
        has_gaps,
        dict(floor_competitors),
        [econ_entry, breadth_entry, wtp_floor_entry(floor_competitors, cfg)],
    )


# --- one judge ------------------------------------------------------------------------------


def validate_judgments(
    rubric: RubricJudgments,
    table: Sequence[Cited],
    code: CodeFactors,
    cfg: ScoreDefaults,
) -> Judge:
    """The judge's seven factors: model factors with citations mapped to database ids and
    capped, then the WTP floor, then the code factors."""
    numbered = dict(enumerate(table, 1))
    judge = Judge({})
    for j in rubric.judgments:
        if j.factor not in MODEL_FACTORS:
            judge.notes["code_factor_judged"] += 1
            continue
        if j.factor in judge.factors:
            judge.notes["repeated_factors"] += 1
            continue
        ids: list[int] = []
        for n in j.claim_ids:
            row = numbered.get(n)
            if row is None:
                judge.notes["invalid_citations"] += 1
            elif row.id not in ids:
                ids.append(row.id)
        score = FactorScore(j.level, "model", j.justification.strip(), ids)
        if not any(numbered[n].kind == "fact" for n in j.claim_ids if n in numbered):
            _cap(judge, j.factor, score, "uncited_cap", cfg.uncited_cap)
        if j.factor == "competition_gap" and not code.has_gaps:
            _cap(judge, j.factor, score, "no_gap_cap", cfg.no_gap_cap)
        judge.factors[j.factor] = score
    for name in MODEL_FACTORS:
        if name not in judge.factors:
            judge.notes["missing_factors"] += 1
            judge.factors[name] = FactorScore(1, "model", missing=True)
            judge.events.append({"rule": "missing_factor", "factor": name, "fired": True,
                                 "inputs": {}, "level": 1})  # fmt: skip
    wtp = judge.factors["willingness_to_pay"]
    if len(code.floor_competitors) >= cfg.wtp_floor.min_competitors and (
        wtp.level < cfg.wtp_floor.level
    ):
        wtp.level, wtp.floor = cfg.wtp_floor.level, True
        for ids in code.floor_competitors.values():
            wtp.claim_ids += [i for i in ids if i not in wtp.claim_ids]
    econ_level, econ_ids = code.economic_impact
    judge.factors["economic_impact"] = FactorScore(econ_level, "code", claim_ids=list(econ_ids))
    judge.factors["market_breadth"] = FactorScore(code.market_breadth, "code")
    return judge


def _cap(judge: Judge, factor: str, score: FactorScore, rule: str, cap: int) -> None:
    if score.level > cap:
        judge.events.append({"rule": rule, "factor": factor, "fired": True,
                             "inputs": {"from": score.level, "to": cap}})  # fmt: skip
        score.level, score.capped = cap, True


# --- merging judges -------------------------------------------------------------------------


def attractiveness(levels: Mapping[str, int], weights: Mapping[str, int]) -> float:
    """Σ weight × (level − 1) / 4, 0–100."""
    return round(sum(w * (levels[f] - 1) / 4 for f, w in weights.items()), 2)


@dataclass
class Merged:
    factors: dict[str, dict[str, Any]]
    levels: dict[str, int]
    attractiveness: float
    max_spread: int | None  # None with a single judge
    judges: int
    trace: list[dict[str, Any]]


def merge_judges(judges: Sequence[Judge], code: CodeFactors, cfg: ScoreDefaults) -> Merged:
    """Per factor the median level (the upper median for an even count) and the spread; the
    citations and justification are the median judge's, so a level above the uncited cap
    always cites a fact."""
    factors: dict[str, dict[str, Any]] = {}
    levels: dict[str, int] = {}
    spreads = []
    for name, weight in cfg.weights.items():
        scores = [j.factors[name] for j in judges]
        ordered = sorted(range(len(scores)), key=lambda i: scores[i].level)
        mid = scores[ordered[len(ordered) // 2]]
        judged = [s.level for s in scores]
        spread = max(judged) - min(judged)
        if name in MODEL_FACTORS:
            spreads.append(spread)
        factors[name] = mid.as_dict(weight, judged, spread)
        levels[name] = mid.level
    trace = list(code.trace)
    for i, j in enumerate(judges):
        trace += [e | {"judge": i} for e in j.events]
    return Merged(
        factors,
        levels,
        attractiveness(levels, cfg.weights),
        max(spreads) if len(judges) > 1 else None,
        len(judges),
        trace,
    )


# --- confidence -----------------------------------------------------------------------------


def hypothesis_share(
    factors: Mapping[str, Mapping[str, Any]],
    claims: Mapping[int, Cited],
    weights: Mapping[str, int],
    role_fact_ids: Collection[int] = (),
) -> tuple[float, dict[str, float], dict[str, str]]:
    """Share of factor weight resting on hypotheses or unsourced assumptions (0–1), per factor,
    and notes on why a factor counts.

    A factor counts fully when its citations include no fact. ``economic_impact`` counts by the
    unsourced share of its assumption claims (fully without any). ``customer_accessibility``
    counts as fact-backed only when it cites a fact one of the opportunity's buyer roles cites
    (``role_fact_ids``): a competitor segment fact shows whom competitors sell to, not that these
    buyers are reachable."""
    by_factor: dict[str, float] = {}
    notes: dict[str, str] = {}
    for name, f in factors.items():
        cited = [claims[i] for i in f["claim_ids"] if i in claims]
        facts = [c for c in cited if c.kind == "fact"]
        if name == "economic_impact":
            assumptions = [c for c in cited if c.kind == "assumption"]
            share = (
                sum(not c.sourced for c in assumptions) / len(assumptions) if assumptions else 1.0
            )
        elif name == "customer_accessibility":
            share = 0.0 if any(c.id in role_fact_ids for c in facts) else 1.0
            if facts and share:
                notes[name] = "cites no fact a buyer role cites; other facts don't show reach"
        else:
            share = 0.0 if facts else 1.0
        by_factor[name] = round(share, 3)
    total = sum(weights[n] * s for n, s in by_factor.items()) / sum(weights.values())
    return round(total, 3), by_factor, notes


def confidence(
    strength: float,
    share: float,
    by_factor: Mapping[str, float],
    max_spread: int | None,
    judges: int,
    cfg: ScoreDefaults,
    share_notes: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """High needs all three inputs within the high band (and ≥ 2 judges, so a spread exists);
    low if any one is outside the low band; medium otherwise."""
    hi, lo = cfg.confidence.high, cfg.confidence.low
    reasons = []
    if strength < lo.min_strength:
        reasons.append(f"strength {strength} < {lo.min_strength}")
    if share > lo.max_hypothesis_share:
        reasons.append(f"hypothesis share {share} > {lo.max_hypothesis_share}")
    if max_spread is not None and max_spread > lo.max_spread:
        reasons.append(f"spread {max_spread} > {lo.max_spread}")
    if reasons:
        result = "low"
    elif (
        strength >= hi.min_strength
        and share <= hi.max_hypothesis_share
        and max_spread is not None
        and max_spread <= hi.max_spread
    ):
        result = "high"
    else:
        result = "medium"
        if strength < hi.min_strength:
            reasons.append(f"strength {strength} < {hi.min_strength}")
        if share > hi.max_hypothesis_share:
            reasons.append(f"hypothesis share {share} > {hi.max_hypothesis_share}")
        if max_spread is None:
            reasons.append("one judge: no spread")
        elif max_spread > hi.max_spread:
            reasons.append(f"spread {max_spread} > {hi.max_spread}")
    entry = {
        "rule": "confidence",
        "result": result,
        "inputs": {"strength": strength, "hypothesis_share": share,
                   "hypothesis_share_by_factor": dict(by_factor), "max_spread": max_spread,
                   "hypothesis_share_notes": dict(share_notes or {}), "judges": judges},
        "thresholds": cfg.confidence.model_dump(),
        "reasons": reasons,
    }  # fmt: skip
    return result, entry


# --- founder fit ----------------------------------------------------------------------------


def founder_fit(
    feasibility: FeasibilityJudgment | None,
    barrier_fact_ids: list[int],
    price_ceiling_high: float | None,
    founder: Mapping[str, Any],
    cfg: ScoreDefaults,
) -> tuple[str, dict[str, Any]]:
    """``not_fit``: a hard barrier backed by a fact, or a price ceiling below the founder's
    minimum. ``stretch``: MVP feasibility ``stretch``/``no``, a sales motion in
    ``stretch_sales_motions``, or no feasibility answer. Otherwise ``fit``."""
    minimum = founder["min_customer_value_usd_month"]
    not_fit, stretch = [], []
    if feasibility and feasibility.hard_barriers and barrier_fact_ids:
        not_fit.append(f"hard barrier backed by facts {barrier_fact_ids}")
    if price_ceiling_high is not None and price_ceiling_high < minimum:
        not_fit.append(f"price ceiling ${price_ceiling_high}/month < ${minimum}/month")
    if feasibility is None:
        stretch.append("feasibility_unavailable")
    else:
        if feasibility.mvp_feasible != "yes":
            stretch.append(f"mvp_feasible {feasibility.mvp_feasible}")
        if feasibility.sales_motion in cfg.stretch_sales_motions:
            stretch.append(f"sales motion {feasibility.sales_motion}")
    result = "not_fit" if not_fit else "stretch" if stretch else "fit"
    inputs: dict[str, Any] = {
        "team": founder.get("team"),
        "mvp_months": founder.get("mvp_months"),
        "min_customer_value_usd_month": minimum,
        "price_ceiling_high": price_ceiling_high,
        "stretch_sales_motions": cfg.stretch_sales_motions,
    }
    if feasibility is not None:
        inputs |= {
            "mvp_feasible": feasibility.mvp_feasible,
            "hard_barriers": feasibility.hard_barriers,
            "barrier_fact_ids": barrier_fact_ids,
            "sales_motion": feasibility.sales_motion,
            "justification": feasibility.justification,
        }
    entry = {"rule": "founder_fit", "result": result, "inputs": inputs,
             "reasons": not_fit or stretch}  # fmt: skip
    return result, entry
