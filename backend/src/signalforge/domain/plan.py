"""Research request (plan §3) and the human-editable research plan, ``plan.yaml`` (plan §4).

``plan.yaml`` embeds the request so it is self-contained: the human checkpoint edits one file, and
``query_gen`` and later stages read everything they need from the plan stored on the run.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class FounderProfile(BaseModel):
    team: str
    budget: str
    mvp_months: int
    preferred_tech: list[str] = []
    min_customer_value_usd_month: float


class ResearchRequest(BaseModel):
    country: str  # selects the market pack
    industry: str  # pack industry id when one exists (e.g. logistics)
    problem_area: str | None = None
    target_customer: str
    business_model: str
    founder: FounderProfile
    report_language: str = "en"
    budget_usd: float = 10.0

    @property
    def pack_id(self) -> str:
        return self.country.lower()


class PlanSubmarket(BaseModel):
    name: str  # English; used in reports and stored as Query.submarket
    name_tr: str  # as practitioners say it, in the pack language
    terms: list[str] = []  # workflows, documents, systems, tools — in the pack language
    actors: list[str] = []  # roles and company types who feel the pain — in the pack language
    notes: str | None = None  # free-text steering from the human reviewer


class ResearchPlan(BaseModel):
    request: ResearchRequest
    submarkets: list[PlanSubmarket] = Field(min_length=1)
    research_questions: list[str] = []
    buyer_hypotheses: list[str] = []
    avoid: list[str] = []  # topics / result types the queries should steer away from

    @model_validator(mode="after")
    def _unique_submarkets(self) -> "ResearchPlan":
        names = [s.name for s in self.submarkets]
        if len(names) != len(set(names)):
            raise ValueError(f"submarket names must be unique: {names}")
        return self


def load_plan(path: Path) -> ResearchPlan:
    with path.open(encoding="utf-8") as f:
        return ResearchPlan.model_validate(yaml.safe_load(f))
