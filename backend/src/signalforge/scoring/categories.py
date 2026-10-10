"""Rule-based category (plan §8.5; agent-modules.md §8), thresholds in ``score.categories``.

Every rule is evaluated in order and appended to the trace with its inputs and whether it fired;
the first that fired is the category. ``interesting`` always fires, so one always matches.
"""

from dataclasses import asdict, dataclass
from typing import Any

from signalforge.config import ScoreDefaults

ORDER = ("false_positive", "weak", "competitive", "strong", "interesting")


@dataclass(frozen=True)
class CategoryInputs:
    supported_facts: int  # counted signal facts with entailment supported or partial
    budget_owner: str  # none | hypothesis | evidence (≥ 1 fact or inference citation)
    price_ceiling_high: float | None  # None without a valid economic model
    min_customer_value: float
    severity: int
    competition_gap: int
    willingness_to_pay: int
    attractiveness: float
    strength: float


def categorize(i: CategoryInputs, cfg: ScoreDefaults) -> tuple[str, list[dict[str, Any]]]:
    c = cfg.categories
    below_minimum = i.price_ceiling_high is not None and i.price_ceiling_high < i.min_customer_value
    fired = {
        "false_positive": i.supported_facts < c.false_positive.min_supported_facts,
        "weak": i.budget_owner == "none" or below_minimum or i.severity <= c.weak.max_severity,
        "competitive": i.competition_gap <= c.competitive.max_gap
        and i.willingness_to_pay >= c.competitive.min_wtp,
        "strong": i.attractiveness >= c.strong.min_attractiveness
        and i.strength >= c.strong.min_strength
        and i.budget_owner == "evidence"
        and i.competition_gap >= c.strong.min_gap,
        "interesting": True,
    }
    values = asdict(i)
    used = {
        "false_positive": ("supported_facts",),
        "weak": ("budget_owner", "price_ceiling_high", "min_customer_value", "severity"),
        "competitive": ("competition_gap", "willingness_to_pay"),
        "strong": ("attractiveness", "strength", "budget_owner", "competition_gap"),
        "interesting": (),
    }
    thresholds = c.model_dump()
    trace = [
        {"rule": f"category.{name}", "fired": fired[name],
         "inputs": {k: values[k] for k in used[name]}, "thresholds": thresholds.get(name, {})}
        for name in ORDER
    ]  # fmt: skip
    return next(name for name in ORDER if fired[name]), trace
