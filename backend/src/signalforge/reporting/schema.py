"""Report schemas (agent-modules.md §9): ``report.json`` is the source of truth.

The model writes ``DraftBullet`` / ``SectionDraft`` / ``SummaryDraft`` (claim numbers local to one
section's table); code maps them to database ids and builds the stored ``Report``. Each stored
section keeps the claim ids its writer was allowed to cite (``table``) and the extra allowed
sources (``extra``), so the validator is pure over a stored file.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from signalforge.domain.plan import ResearchRequest

SectionKey = Literal[
    "problem",
    "evidence",
    "who_has_it",
    "current_solutions",
    "gaps",
    "buyers",
    "economics",
    "risks",
    "proposed_product",
    "mvp",
    "validation_experiment",
]
SECTION_KEYS: tuple[str, ...] = (
    "problem", "evidence", "who_has_it", "current_solutions", "gaps", "buyers", "economics",
    "risks", "proposed_product", "mvp", "validation_experiment",
)  # fmt: skip
UNCITED_ALLOWED = {"proposed_product", "mvp", "validation_experiment"}  # labelled recommendations
BulletKind = Literal["fact", "inference", "hypothesis", "assumption", "recommendation"]
SUMMARY = "summary"  # the run summary is validated like a section outside UNCITED_ALLOWED


class Bullet(BaseModel):
    text: str
    claim_ids: list[int]  # empty only in UNCITED_ALLOWED sections
    kind: BulletKind


class Section(BaseModel):
    key: SectionKey
    bullets: list[Bullet]
    table: list[int] = []  # claim ids the writer could cite
    extra: list[str] = []  # allowed sources besides the cited claims (economic model, card, ...)


class OpportunityReport(BaseModel):
    opportunity_id: int
    title: str
    one_liner: str
    category: str
    attractiveness: float | None
    confidence: str | None
    founder_fit: str | None
    sections: list[Section]


class OpportunitySummary(BaseModel):
    opportunity_id: int
    segment: str
    category: str
    attractiveness: float | None
    confidence: str | None
    founder_fit: str | None
    fired_rule: str | None
    top_factor: str | None  # the factor with the highest weight × level


class RankingRow(BaseModel):
    """One full report in the summary's ranking line, straight from its score card."""

    opportunity_id: int
    title: str
    category: str
    attractiveness: float | None
    confidence: str | None
    founder_fit: str | None


class Reason(BaseModel):
    rule: str
    text: str
    detail: str | None = None


class DontBuildItem(BaseModel):
    opportunity_id: int
    segment: str
    category: str
    reasons: list[Reason]


class ExcerptView(BaseModel):
    quote: str  # original language, ≤ excerpt_max_chars
    translation: str | None
    url: str | None
    domain: str | None
    date: str | None


class QuoteView(ExcerptView):
    statement: str  # the signal as extracted


class InsufficientItem(BaseModel):
    cluster_id: int
    name: str
    why: str
    failed_rules: list[str]
    signals: int
    independent_sources: int
    strength: float | None
    quotes: list[QuoteView]


class ClaimView(BaseModel):
    id: int
    kind: str
    statement: str
    entailment: str | None
    derived_from: list[int] = []
    excerpts: list[ExcerptView] = []
    meta: dict[str, Any] = {}


class Dropped(BaseModel):
    opportunity_id: int | None
    section: str
    text: str
    claim_ids: list[int]
    errors: list[str]
    rules: list[str] = []


class MethodView(BaseModel):
    pack: str  # the pack loaded for this report (economics used the current pack too)
    run_pack: str  # the pack version stored on the run when it was created
    counts: dict[str, int | None]
    stage_costs: dict[str, float]
    models: dict[str, list[str]]
    prompts: list[str]
    sections_generated: int = 0
    regenerations: int = 0
    failed_sections: list[str] = []
    dropped_bullets: list[Dropped] = []
    notes: list[str] = []


class Report(BaseModel):
    run_id: int
    generated_at: datetime
    request: ResearchRequest
    summary_ranking: list[RankingRow] = []  # deterministic, from the cards
    summary: list[Bullet]
    summary_table: list[int] = []
    summary_extra: list[str] = []
    opportunities: list[OpportunityReport]
    other_opportunities: list[OpportunitySummary]
    dont_build: list[DontBuildItem]
    insufficient_evidence: list[InsufficientItem]
    claims: list[ClaimView]
    method: MethodView


# --- what the model writes (claim numbers are local to the prompt's table) ------------------


class DraftBullet(BaseModel):
    text: str = Field(description="English, ≤ 40 words; numbers and names only from cited claims")
    claim_ids: list[int] = Field(description="numbers of the table claims the bullet rests on")
    kind: BulletKind


class SectionDraft(BaseModel):
    bullets: list[DraftBullet]


class SummaryDraft(BaseModel):
    bullets: list[DraftBullet]
