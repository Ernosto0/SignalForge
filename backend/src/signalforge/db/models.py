"""ORM tables for the evidence graph (plan §5), the LLM cost ledger, and the provider cache.

Enumerated values are stored as short strings; the allowed values are listed in comments and
enforced by the Pydantic domain layer, so adding a value never needs a migration.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from signalforge.db.base import Base

Money = Numeric(14, 6)


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _run_fk() -> Mapped[int]:
    return mapped_column(ForeignKey("research_runs.id", ondelete="CASCADE"), index=True)


# --- runs -----------------------------------------------------------------------------------


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[int] = _pk()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    status: Mapped[str] = mapped_column(String(32), default="created")
    # created | planned | running | stopped (requested stages done; resumable) | paused_budget |
    # failed | completed
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pack_id: Mapped[str] = mapped_column(String(16))
    pack_version: Mapped[str] = mapped_column(String(32))
    config_hash: Mapped[str] = mapped_column(String(64))
    budget_usd: Mapped[Decimal] = mapped_column(Money)
    spent_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))


class StageRun(Base):
    __tablename__ = "stage_runs"
    __table_args__ = (Index("ix_stage_runs_run_id_stage", "run_id", "stage"),)

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = mapped_column(ForeignKey("research_runs.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # running | completed | failed | skipped
    input_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    error: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


# --- collection -----------------------------------------------------------------------------


class Query(Base):
    __tablename__ = "queries"
    __table_args__ = (UniqueConstraint("run_id", "text"),)

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    text: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(8))
    intent: Mapped[str] = mapped_column(
        String(16)
    )  # pain | verify | competitor | regulatory | jobs
    submarket: Mapped[str | None] = mapped_column(Text)
    # {signal_type, source_hint, origin: llm | pack_seed} — lets query yield be broken down later
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = _created_at()


class SearchResult(Base):
    __tablename__ = "search_results"

    id: Mapped[int] = _pk()
    query_id: Mapped[int] = mapped_column(ForeignKey("queries.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    rank: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32))


class Document(Base):
    """A source page. Full text lives in the page cache (never exported), keyed by canonical URL."""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("run_id", "canonical_url"),)

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    source_category: Mapped[str | None] = mapped_column(String(32))
    quality_tier: Mapped[str | None] = mapped_column(String(8))  # high | medium | low
    snippet_only: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    text_hash: Mapped[str | None] = mapped_column(String(64))
    lang: Mapped[str | None] = mapped_column(String(8))
    # Which stage stored it: collect (search → fetch) | verify | competitor (the M4 loops).
    origin: Mapped[str] = mapped_column(String(16), default="collect", server_default="collect")
    # The problem cluster a loop document was found for (null for collected documents).
    problem_id: Mapped[int | None] = mapped_column(
        ForeignKey("problem_clusters.id", ondelete="SET NULL")
    )


class UrlCandidate(Base):
    """One unique (canonical) URL from a run's search results, with its triage decision and fetch
    outcome. Search results collapse into candidates; kept candidates become documents."""

    __tablename__ = "url_candidates"
    __table_args__ = (UniqueConstraint("run_id", "canonical_url"),)

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    canonical_url: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)  # best-ranked original URL
    domain: Mapped[str] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    source_category: Mapped[str | None] = mapped_column(String(32))
    quality_tier: Mapped[str] = mapped_column(String(8))  # high | medium | low
    access: Mapped[str] = mapped_column(String(16))  # fetch | snippet_only
    query_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    best_rank: Mapped[int] = mapped_column(Integer)
    # True when every query that found it carried a `site:` hint for another domain (Google
    # silently drops `site:` when nothing on the site matches).
    off_site: Mapped[bool] = mapped_column(Boolean, default=False)
    decision: Mapped[str] = mapped_column(String(8))  # keep | reserve | drop
    # prefilter rule (skip_domain | file_type) or llm | below_min_score | unjudged
    decision_rule: Mapped[str] = mapped_column(String(24))
    triage_score: Mapped[int | None] = mapped_column(Integer)  # 0–3
    triage_label: Mapped[str | None] = mapped_column(String(24))
    triage_reason: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int | None] = mapped_column(Integer)  # fetch order among keep + reserve
    fetch_status: Mapped[str | None] = mapped_column(String(24))  # FetchStatus | snippet_only | …
    fetch_error: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))


# --- evidence -------------------------------------------------------------------------------


class Excerpt(Base):
    __tablename__ = "excerpts"

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    # Original language, verbatim: the matched span of the source text, not the model's copy.
    quote: Mapped[str] = mapped_column(Text)
    translation: Mapped[str | None] = mapped_column(Text)
    # Offsets into the source text (page text, or "title\nsnippet" for snippet-only documents).
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(8), default="text")  # text | snippet
    verified: Mapped[str] = mapped_column(String(8))  # exact | fuzzy
    author_hash: Mapped[str | None] = mapped_column(String(64))  # salted; never the raw name
    # The stage that wrote it (extract | verify | competitors); stages delete only their own.
    stage: Mapped[str] = mapped_column(String(32), default="extract", server_default="extract")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    excerpt_id: Mapped[int] = mapped_column(
        ForeignKey("excerpts.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(16))
    # complaint | workaround | labor_spend | wish | tool_complaint | price_signal | regulatory
    actor: Mapped[str | None] = mapped_column(Text)
    workflow: Mapped[str | None] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text)
    first_hand: Mapped[bool] = mapped_column(Boolean)
    # {submarket}; verify adds {origin: "verify", problem_id, counter: bool}
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class IndependenceGroup(Base):
    __tablename__ = "independence_groups"

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    rule: Mapped[str] = mapped_column(
        String(16)
    )  # same_author | same_quote | syndicated | near_dup
    document_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))


class ProblemCluster(Base):
    __tablename__ = "problem_clusters"

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    signal_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    independent_source_count: Mapped[int] = mapped_column(Integer, default=0)
    source_category_mix: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    signal_type_mix: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    evidence_strength: Mapped[float | None] = mapped_column(Float)
    strength: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # component breakdown
    # Gate 1 (shortlist stage): [{rule, passed, value, threshold}] so every decision is explainable.
    shortlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    gate_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    rank: Mapped[int | None] = mapped_column(Integer)  # by evidence strength, 1 = strongest
    # The cluster's inference claim ("<actor> has <problem>"), derived from its signals' facts.
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("claims.id", ondelete="SET NULL"))
    # verify stage: what the second round added and how the evidence changed; the Gate-1 columns
    # above are left as shortlist computed them (evidence/clusters.py reads both).
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_run_id_stage", "run_id", "stage"),
        Index("ix_claims_run_id_kind", "run_id", "kind"),
    )

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    kind: Mapped[str] = mapped_column(String(16))  # fact | inference | hypothesis | assumption
    statement: Mapped[str] = mapped_column(Text)
    supports: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)  # excerpt ids
    derived_from: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)  # claim ids
    stage: Mapped[str] = mapped_column(String(32))
    entailment_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    # supported | partial | not_supported | contradicted (evidence/entailment.py); null = unchecked
    entailment: Mapped[str | None] = mapped_column(String(16))
    # Per kind: facts from competitors carry {competitor_id, kind}; entailment adds
    # {entailment_note, entailment_by}; assumptions (M5) carry their dated values.
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


# --- analysis -------------------------------------------------------------------------------


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    problem_id: Mapped[int] = mapped_column(
        ForeignKey("problem_clusters.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    segment: Mapped[str | None] = mapped_column(Text)
    geo: Mapped[str | None] = mapped_column(String(64))
    # [{amount, currency, period, plan_name, observed_at, claim_id}]; never converted here
    pricing: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)


class GapMatrix(Base):
    __tablename__ = "gap_matrices"

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    problem_id: Mapped[int] = mapped_column(
        ForeignKey("problem_clusters.id", ondelete="CASCADE"), unique=True
    )
    # {dimensions: [{key, label, from_signal_ids}], competitor_ids: [...],
    #  cells: {dim: {competitor_id: {value, claim_id}}}, gaps: [{dimension, claim_id}]}
    matrix: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = _pk()
    run_id: Mapped[int] = _run_fk()
    problem_id: Mapped[int] = mapped_column(
        ForeignKey("problem_clusters.id", ondelete="CASCADE"), index=True
    )
    segment: Mapped[str] = mapped_column(Text)
    solution_angle: Mapped[str] = mapped_column(Text)
    # buyers stage: {user|buyer|decision_maker|economic_beneficiary: {role, claim_ids, hypothesis,
    # hypothesis_claim_id}, budget_owner: null | {…}, gap_claim_ids: [...]}
    buyer_roles: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    economic_model: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    wtp_signals: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    status: Mapped[str | None] = mapped_column(String(16))  # monetization (Gate 2)
    knockouts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    # {channels: [{kind, name, claim_ids, cited}]}
    accessibility: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    # {hint, status: not_searched}; breadth search is deferred
    market_breadth: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")


class ScoreCard(Base):
    __tablename__ = "score_cards"

    id: Mapped[int] = _pk()
    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), unique=True
    )
    factors: Mapped[dict[str, Any]] = mapped_column(JSONB)  # level, justification, claim_ids
    # The next three are null on a knocked-out opportunity's card (not assessed).
    attractiveness: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[str | None] = mapped_column(String(8))  # low | medium | high
    founder_fit: Mapped[str | None] = mapped_column(String(8))  # fit | stretch | not_fit
    category: Mapped[str] = mapped_column(String(32))
    rule_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # {claim_id, factor, importance, uncertainty, score, name, cost_usd, duration_days, pass_fail}
    experiment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


# --- ledger, labels, cache ------------------------------------------------------------------


class LLMCall(Base):
    """Cost ledger: one row per LLM request, including cache hits (cost 0) and failures."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = _pk()
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_runs.id", ondelete="SET NULL"), index=True
    )
    stage: Mapped[str | None] = mapped_column(String(32))
    tier: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    prompt_id: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[int] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    response_id: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Label(Base):
    __tablename__ = "labels"
    __table_args__ = (Index("ix_labels_target", "target_type", "target_id"),)

    id: Mapped[int] = _pk()
    target_type: Mapped[str] = mapped_column(String(16))  # signal | cluster | opportunity
    target_id: Mapped[int] = mapped_column(BigInteger)
    labeler: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class CacheEntry(Base):
    """Record/replay cache for search responses, fetched pages and LLM responses (plan §11)."""

    __tablename__ = "cache_entries"

    namespace: Mapped[str] = mapped_column(String(16), primary_key=True)  # search | page | llm
    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the request
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)  # what produced it, for debugging
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
