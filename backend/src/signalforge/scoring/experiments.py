"""Validation experiment (plan §8.6; agent-modules.md §8).

For each claim a card cites: importance = Σ weight of the factors citing it, uncertainty from
its kind (``score.uncertainty``). The claim with the highest importance × uncertainty (ties:
heavier factor, then lower claim id) is the one to test; the experiment is the cheapest in
``experiments.yaml`` (cash cost, then duration) whose ``tests`` include that claim's heaviest
factor that has one.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from string import Formatter
from typing import Any

import yaml
from pydantic import BaseModel

CATALOGUE = Path(__file__).parent / "experiments.yaml"
FIELDS = frozenset({"segment", "statement", "price_usd"})


class Experiment(BaseModel):
    name: str
    cost_usd: float
    duration_days: int
    tests: list[str]
    pass_fail: str


@cache
def load_catalogue(path: Path = CATALOGUE) -> tuple[Experiment, ...]:
    """The catalogue; a template with an unknown placeholder fails here, not mid-run."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = tuple(Experiment.model_validate(e) for e in raw["experiments"])
    for e in out:
        unknown = {f for _, f, _, _ in Formatter().parse(e.pass_fail) if f} - FIELDS
        if unknown:
            raise ValueError(f"experiment {e.name}: unknown placeholders {sorted(unknown)}")
    return out


def uncertainty_key(kind: str, entailment: str | None, sourced: bool | None) -> str:
    """The ``score.uncertainty`` key of a claim."""
    if kind == "assumption":
        return "assumption_sourced" if sourced else "assumption_unsourced"
    if kind == "fact":
        return {"supported": "fact_supported", "partial": "fact_partial"}.get(
            entailment or "", "fact_unchecked"
        )
    return kind  # hypothesis | inference


@dataclass(frozen=True)
class Candidate:
    claim_id: int
    factors: tuple[str, ...]  # heaviest first
    importance: int
    uncertainty: float

    @property
    def score(self) -> float:
        return round(self.importance * self.uncertainty, 3)


def rank_claims(
    cited: Mapping[str, list[int]],
    keys: Mapping[int, str],
    weights: Mapping[str, int],
    uncertainty: Mapping[str, float],
) -> list[Candidate]:
    """Claims cited by any factor (``cited``: factor -> claim ids), best first."""
    factors_of: dict[int, list[str]] = {}
    for factor, ids in cited.items():
        for i in ids:
            factors_of.setdefault(i, []).append(factor)
    out = []
    for claim_id, factors in factors_of.items():
        if claim_id not in keys:
            continue
        ordered = tuple(sorted(factors, key=lambda f: -weights[f]))
        out.append(
            Candidate(
                claim_id, ordered, sum(weights[f] for f in factors), uncertainty[keys[claim_id]]
            )
        )
    return sorted(out, key=lambda c: (-c.score, -weights[c.factors[0]], c.claim_id))


def pick_experiment(
    candidates: list[Candidate],
    catalogue: tuple[Experiment, ...],
    fields: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """The experiment for the best candidate with a matching catalogue entry. ``fields`` gives
    each claim's template values (segment, statement, price_usd)."""
    for c in candidates:
        for factor in c.factors:
            matching = [e for e in catalogue if factor in e.tests]
            if not matching:
                continue
            e = min(matching, key=lambda e: (e.cost_usd, e.duration_days))
            return {
                "claim_id": c.claim_id,
                "factor": factor,
                "importance": c.importance,
                "uncertainty": c.uncertainty,
                "score": c.score,
                "name": e.name,
                "cost_usd": e.cost_usd,
                "duration_days": e.duration_days,
                "pass_fail": e.pass_fail.format(**fields[c.claim_id]),
            }
    return None
