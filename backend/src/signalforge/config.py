from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent


class CacheMode(StrEnum):
    LIVE = "live"  # serve fresh cache entries, otherwise call the provider and store
    RECORD = "record"  # always call the provider and overwrite the cache
    REPLAY = "replay"  # cache only; a miss is an error


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://signalforge:signalforge@localhost:5433/signalforge"

    openai_api_key: SecretStr | None = None
    llm_model_fast: str = "gpt-6-luna"
    llm_model_analysis: str = "gpt-6-luna"  # dev default; gpt-6.1-sol for eval runs (plan §10)
    llm_model_synthesis: str = "gpt-6-luna"
    jev_api_key: SecretStr | None = None  # reserved, see plan §10
    # Salts author hashes (independence counting, plan §2 #17); raw names are never stored.
    author_hash_salt: SecretStr | None = None

    search_provider: str = "serpapi"
    serp_api_key: SecretStr | None = None

    default_market_pack: str = "tr"
    default_budget_usd: float = 10.0
    cache_mode: CacheMode = CacheMode.LIVE

    market_packs_dir: Path = BACKEND_DIR / "market_packs"
    defaults_file: Path = BACKEND_DIR / "config" / "defaults.yaml"


class ModelPrice(BaseModel):
    """USD per 1M tokens."""

    input: Decimal
    cached_input: Decimal
    output: Decimal


class CacheDefaults(BaseModel):
    ttl_days: dict[str, int | None]


class SearchDefaults(BaseModel):
    results_per_query: int
    timeout_s: float


class FetchDefaults(BaseModel):
    user_agent: str
    timeout_s: float
    max_bytes: int
    per_domain_interval_s: float
    min_text_chars: int


class LLMDefaults(BaseModel):
    timeout_s: float
    max_output_tokens: int
    pricing: dict[str, ModelPrice]


class QueryGenDefaults(BaseModel):
    max_queries: int
    intent_mix: dict[str, float]  # intent -> share of the per-submarket quota
    pain_signal_mix: dict[str, float]  # signal type -> share of the pain quota
    site_hint_share: dict[str, float]  # intent -> share of drafts asked to carry a `site:` hint
    source_categories: dict[str, list[str]]  # intent -> registry categories offered as hints
    overgenerate: float
    min_words: int
    max_words: int
    max_quoted_phrases: int
    near_dup_ratio: float
    pack_seed_queries: bool
    concurrency: int
    max_output_tokens: int


class CollectDefaults(BaseModel):
    search_concurrency: int
    search_retries: int


class TriageDefaults(BaseModel):
    max_urls: int
    max_per_domain: int
    min_score: dict[str, int]  # source tier -> minimum triage score to keep
    skip_domains: list[str]
    skip_extensions: list[str]
    batch_size: int
    concurrency: int
    max_output_tokens: int


class FetchStageDefaults(BaseModel):
    max_documents: int
    max_attempts: int
    concurrency: int


class DedupeDefaults(BaseModel):
    shingle_words: int
    num_perm: int
    near_dup_threshold: float
    min_words: int


class ExtractDefaults(BaseModel):
    chunk_chars: int
    chunk_overlap_chars: int
    max_chunks_per_doc: int
    max_signals_per_chunk: int
    min_quote_chars: int
    max_quote_chars: int
    fuzzy_min_ratio: float
    concurrency: int
    max_output_tokens: int


class ClusterDefaults(BaseModel):
    max_clusters: int
    max_signals_per_call: int
    min_cluster_size: int
    keep_regulatory_singletons: bool
    max_output_tokens: int


class StrengthDefaults(BaseModel):
    weights: dict[str, float]  # component -> weight; sums to 1
    source_saturation: int
    diversity_saturation: int
    tier_weights: dict[str, float]
    snippet_only_factor: float
    recency_months: int
    unknown_date: float


class ShortlistDefaults(BaseModel):
    min_strength: float
    min_independent_sources: int
    max_shortlisted: int


class Defaults(BaseModel):
    """Typed view of config/defaults.yaml."""

    cache: CacheDefaults
    search: SearchDefaults
    fetch: FetchDefaults
    llm: LLMDefaults
    query_gen: QueryGenDefaults
    collect: CollectDefaults
    triage: TriageDefaults
    fetch_stage: FetchStageDefaults
    dedupe: DedupeDefaults
    extract: ExtractDefaults
    cluster: ClusterDefaults
    strength: StrengthDefaults
    shortlist: ShortlistDefaults


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_defaults() -> Defaults:
    with get_settings().defaults_file.open(encoding="utf-8") as f:
        return Defaults.model_validate(yaml.safe_load(f))
