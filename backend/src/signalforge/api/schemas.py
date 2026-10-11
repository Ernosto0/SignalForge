"""API response models (the M8 contract, .claude/m8-spec.md). Times are ISO 8601 UTC, money is a
JSON number (USD). Stored JSON blobs are passed through as stored; object keys that are ids are
strings. Never page text or author hashes (plan §2 #17): excerpts and URLs only."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, PlainSerializer

from signalforge.domain.plan import ResearchPlan


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UTCDateTime = Annotated[datetime, PlainSerializer(_utc, return_type=str)]
StageStatus = Literal["pending", "running", "completed", "failed"]
AgentStatus = Literal["pending", "running", "completed", "failed", "partial"]


class AgentOut(BaseModel):
    name: str
    description: str
    stages: list[str]


class PlanExample(BaseModel):
    id: str  # path under backend/examples without ``.plan.yaml``, e.g. "m7/customs"
    title: str
    plan: ResearchPlan


class JobOut(BaseModel):
    id: int
    run_id: int
    status: str  # queued | running | completed | failed | cancelled
    args: dict[str, Any]
    created_at: UTCDateTime
    started_at: UTCDateTime | None
    finished_at: UTCDateTime | None
    error: str | None


class RunSummary(BaseModel):
    id: int
    created_at: UTCDateTime
    updated_at: UTCDateTime
    status: str
    country: str
    industry: str
    problem_area: str | None
    budget_usd: float
    spent_usd: float
    current_stage: str | None
    stages_done: int
    stages_total: int
    latest_job: JobOut | None
    opportunity_count: int


class StageOut(BaseModel):
    name: str
    status: StageStatus
    started_at: UTCDateTime | None
    finished_at: UTCDateTime | None
    cost_usd: float
    error: str | None
    metrics: dict[str, Any]


class AgentProgress(BaseModel):
    name: str
    description: str
    status: AgentStatus
    cost_usd: float
    started_at: UTCDateTime | None
    finished_at: UTCDateTime | None
    stages: list[StageOut]


class RunDetail(RunSummary):
    plan: dict[str, Any] | None
    pack_id: str
    pack_version: str
    agents: list[AgentProgress]
    jobs: list[JobOut]  # newest first
    report_formats: list[str]  # report files that exist: json, md, html


class ValueRange(BaseModel):
    low: float
    high: float


class OpportunityCard(BaseModel):
    id: int
    run_id: int
    problem_id: int
    problem_name: str
    segment: str
    solution_angle: str
    gate2_status: str | None  # passed | knocked_out
    knockouts: list[dict[str, Any]]
    category: str | None
    attractiveness: float | None
    confidence: str | None
    founder_fit: str | None
    evidence_strength: float | None
    value_usd_month: ValueRange | None


class ExcerptRef(BaseModel):
    id: int
    quote: str
    translation: str | None
    verified: str
    url: str
    domain: str
    source_category: str | None
    quality_tier: str | None


class ClaimOut(BaseModel):
    id: int
    kind: str
    statement: str
    entailment: str | None
    derived_from: list[int]
    excerpts: list[ExcerptRef]


class ProblemOut(BaseModel):
    id: int
    name: str
    description: str
    independent_source_count: int
    evidence_strength: float | None
    source_category_mix: dict[str, Any]
    claim_id: int | None  # the problem's inference claim ("<actor> has <problem>")


class ScoreOut(BaseModel):
    factors: dict[str, Any]
    rule_trace: list[dict[str, Any]]
    experiment: dict[str, Any] | None


class CompetitorRef(BaseModel):
    id: int
    name: str
    url: str | None


class GapMatrixOut(BaseModel):
    dimensions: list[dict[str, Any]]
    competitors: list[CompetitorRef]
    cells: dict[str, Any]  # {dimension: {"<competitor id>": {value, claim_id}}}
    gaps: list[dict[str, Any]]


class OpportunityDetail(OpportunityCard):
    problem: ProblemOut
    buyer_roles: dict[str, Any]
    accessibility: dict[str, Any]
    market_breadth: dict[str, Any]
    economic_model: dict[str, Any]
    wtp_signals: list[dict[str, Any]]
    score: ScoreOut | None
    gap_matrix: GapMatrixOut | None
    claims: dict[str, ClaimOut]  # every claim id referenced in the payload, plus derived_from


class SourceDocument(BaseModel):
    id: int
    url: str
    domain: str
    title: str | None
    source_category: str | None
    quality_tier: str | None
    published_at: UTCDateTime | None
    snippet_only: bool
    origin: str | None


class SourceSignal(BaseModel):
    id: int
    type: str
    statement: str
    first_hand: bool
    cluster_ids: list[int]


class SourceExcerpt(BaseModel):
    id: int
    quote: str
    translation: str | None
    verified: str
    signals: list[SourceSignal]


class SourceItem(BaseModel):
    document: SourceDocument
    excerpts: list[SourceExcerpt]


class ClusterFacet(BaseModel):
    id: int
    name: str
    n: int


class Facets(BaseModel):
    categories: dict[str, int]  # documents per source category
    tiers: dict[str, int]  # documents per quality tier
    signal_types: dict[str, int]  # signals per type
    clusters: list[ClusterFacet]  # signals per problem cluster


class SourcePage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[SourceItem]
    facets: Facets
