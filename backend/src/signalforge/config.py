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
    llm_model_analysis: str = "gpt-6.1-sol"
    llm_model_synthesis: str = "gpt-6.1-sol"
    jev_api_key: SecretStr | None = None  # reserved, see plan §10

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


class Defaults(BaseModel):
    """Typed view of config/defaults.yaml."""

    cache: CacheDefaults
    search: SearchDefaults
    fetch: FetchDefaults
    llm: LLMDefaults


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_defaults() -> Defaults:
    with get_settings().defaults_file.open(encoding="utf-8") as f:
        return Defaults.model_validate(yaml.safe_load(f))
