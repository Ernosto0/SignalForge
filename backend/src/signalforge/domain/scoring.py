"""Stage I/O of ``score`` (agent-modules.md §8): rubric judgments and MVP feasibility.

Claim numbers the model cites are local to one prompt's claim table (1..n), never database ids;
the stage maps them back. Code sets ``economic_impact`` and ``market_breadth``; the model judges
the other five factors.
"""

from typing import Literal

from pydantic import BaseModel, Field

Factor = Literal[
    "severity",
    "economic_impact",
    "frequency",
    "competition_gap",
    "willingness_to_pay",
    "customer_accessibility",
    "market_breadth",
]


class FactorJudgment(BaseModel):
    factor: Factor
    level: int = Field(ge=1, le=5)
    justification: str = Field(description="English, ≤ 40 words; say what the cited claims show")
    claim_ids: list[int] = Field(description="numbers of the table claims this level rests on")


class RubricJudgments(BaseModel):
    judgments: list[FactorJudgment] = Field(description="one per factor you are asked to judge")


class FeasibilityJudgment(BaseModel):
    mvp_feasible: Literal["yes", "stretch", "no"]
    hard_barriers: list[str] = Field(
        description="licences, certifications, deep integrations; English; empty if none"
    )
    barrier_claim_ids: list[int] = Field(description="numbers of table claims stating a barrier")
    sales_motion: Literal["self_serve", "inside_sales", "field_sales", "enterprise"]
    justification: str = Field(description="English, ≤ 40 words")
