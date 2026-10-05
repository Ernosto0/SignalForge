"""Structured LLM calls with cost ledger, record/replay cache and budget guard (plan §10).

Stages call :class:`LLMService` with a *tier*, never a model id. The provider behind it is an
:class:`LLMClient`; V0 has one implementation (OpenAI Responses API).
"""

import time
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from openai import OpenAI
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import ModelPrice, Settings
from signalforge.db.models import LLMCall, ResearchRun
from signalforge.prompts import Prompt
from signalforge.providers.cache import CacheStore, cache_key


class ModelTier(StrEnum):
    FAST = "fast"
    ANALYSIS = "analysis"
    SYNTHESIS = "synthesis"


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    """The run has spent its budget; the runner stops cleanly and the run can be resumed."""


@dataclass(frozen=True)
class Completion:
    parsed: dict[str, Any] | None  # None on refusal / truncated output
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    response_id: str | None = None
    error: str | None = None


class LLMClient(Protocol):
    def parse(
        self,
        *,
        model: str,
        instructions: str,
        input: str,
        schema: type[BaseModel],
        max_output_tokens: int,
    ) -> Completion: ...


class OpenAIClient:
    def __init__(
        self, api_key: str | None, timeout_s: float = 120, client: OpenAI | None = None
    ) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client = client

    def parse(
        self,
        *,
        model: str,
        instructions: str,
        input: str,
        schema: type[BaseModel],
        max_output_tokens: int,
    ) -> Completion:
        if self._client is None:
            if not self._api_key:
                raise LLMError("no OpenAI API key configured (set OPENAI_API_KEY in .env)")
            self._client = OpenAI(api_key=self._api_key, timeout=self._timeout_s)
        raw = self._client.responses.with_raw_response.parse(
            model=model,
            instructions=instructions,
            input=input,
            text_format=schema,
            max_output_tokens=max_output_tokens,
            store=False,
        )
        try:
            response = raw.parse()
        except ValidationError as exc:
            # Output cut off at max_output_tokens (or otherwise not valid JSON): the SDK raises
            # while parsing, but the tokens were spent, so report them from the raw body.
            body = raw.http_response.json()
            usage_body = body.get("usage") or {}
            return Completion(
                parsed=None,
                input_tokens=usage_body.get("input_tokens", 0),
                cached_tokens=(usage_body.get("input_tokens_details") or {}).get(
                    "cached_tokens", 0
                ),
                output_tokens=usage_body.get("output_tokens", 0),
                response_id=body.get("id"),
                error=(
                    f"invalid structured output (status={body.get('status')}, "
                    f"details={body.get('incomplete_details')}): {exc.errors()[0]['msg']}"
                ),
            )
        usage = response.usage
        parsed = response.output_parsed
        error = None
        if parsed is None:
            details = response.incomplete_details
            error = f"no parsed output (status={response.status}, details={details})"
        return Completion(
            parsed=parsed.model_dump(mode="json") if parsed is not None else None,
            input_tokens=usage.input_tokens if usage else 0,
            cached_tokens=usage.input_tokens_details.cached_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            response_id=response.id,
            error=error,
        )


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    output: T
    model: str
    cost_usd: Decimal
    cache_hit: bool
    call_id: int


def compute_cost(price: ModelPrice, input_tokens: int, cached: int, output: int) -> Decimal:
    per_token = (
        (input_tokens - cached) * price.input + cached * price.cached_input + output * price.output
    )
    return (per_token / Decimal(1_000_000)).quantize(Decimal("0.000001"))


class LLMService:
    def __init__(
        self,
        client: LLMClient,
        cache: CacheStore,
        session_factory: sessionmaker[Session],
        settings: Settings,
        pricing: dict[str, ModelPrice],
        max_output_tokens: int,
        run_id: int | None = None,
    ) -> None:
        self._client = client
        self._cache = cache
        self._session_factory = session_factory
        self._models = {
            ModelTier.FAST: settings.llm_model_fast,
            ModelTier.ANALYSIS: settings.llm_model_analysis,
            ModelTier.SYNTHESIS: settings.llm_model_synthesis,
        }
        self._pricing = pricing
        self._max_output_tokens = max_output_tokens
        self.run_id = run_id

    def parse[T: BaseModel](
        self,
        tier: ModelTier,
        prompt: Prompt,
        input: str,
        schema: type[T],
        *,
        stage: str | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResult[T]:
        """``max_output_tokens`` overrides the configured default for calls with large outputs."""
        model = self._models[tier]
        max_output_tokens = max_output_tokens or self._max_output_tokens
        price = self._pricing.get(model)
        if price is None:
            raise LLMError(f"no pricing for model {model!r} in config/defaults.yaml (llm.pricing)")

        input_hash = cache_key(
            {
                "instructions": prompt.text,
                "input": input,
                "schema": schema.model_json_schema(),
                "max_output_tokens": max_output_tokens,
            }
        )
        ledger = {
            "run_id": self.run_id,
            "stage": stage,
            "tier": tier.value,
            "model": model,
            "prompt_id": prompt.id,
            "prompt_version": prompt.version,
            "input_hash": input_hash,
        }

        def call() -> dict[str, Any]:
            self._check_budget()
            started = time.perf_counter()
            try:
                completion = self._client.parse(
                    model=model,
                    instructions=prompt.text,
                    input=input,
                    schema=schema,
                    max_output_tokens=max_output_tokens,
                )
            except Exception as exc:
                self._record(ledger, error=f"{type(exc).__name__}: {exc}")
                raise
            cost = compute_cost(
                price, completion.input_tokens, completion.cached_tokens, completion.output_tokens
            )
            return {
                "parsed": completion.parsed,
                "input_tokens": completion.input_tokens,
                "cached_tokens": completion.cached_tokens,
                "output_tokens": completion.output_tokens,
                "cost_usd": str(cost),
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "response_id": completion.response_id,
                "error": completion.error,
            }

        cached = self._cache.get_or_compute(
            "llm",
            {"model": model, "prompt": prompt.ref, "input_hash": input_hash},
            call,
            cacheable=lambda r: r["parsed"] is not None,
        )
        payload = cached.response
        cost = Decimal(0) if cached.hit else Decimal(payload["cost_usd"])
        call_id = self._record(
            ledger,
            input_tokens=payload["input_tokens"],
            cached_tokens=payload["cached_tokens"],
            output_tokens=payload["output_tokens"],
            cost_usd=cost,
            latency_ms=0 if cached.hit else payload["latency_ms"],
            cache_hit=cached.hit,
            response_id=payload["response_id"],
            error=payload["error"],
        )
        if payload["parsed"] is None:
            raise LLMError(f"{prompt.ref} on {model}: {payload['error']}")
        return LLMResult(
            output=schema.model_validate(payload["parsed"]),
            model=model,
            cost_usd=cost,
            cache_hit=cached.hit,
            call_id=call_id,
        )

    def _check_budget(self) -> None:
        if self.run_id is None:
            return
        with self._session_factory() as session:
            row = session.execute(
                select(ResearchRun.spent_usd, ResearchRun.budget_usd).where(
                    ResearchRun.id == self.run_id
                )
            ).one()
        if row.spent_usd >= row.budget_usd:
            raise BudgetExceeded(
                f"run {self.run_id} spent ${row.spent_usd} of ${row.budget_usd} budget"
            )

    def _record(self, ledger: dict[str, Any], **fields: Any) -> int:
        """Write the ledger row and add its cost to the run, in one transaction."""
        with self._session_factory.begin() as session:
            call = LLMCall(**ledger, **fields)
            session.add(call)
            cost = fields.get("cost_usd") or Decimal(0)
            if self.run_id is not None and cost:
                session.execute(
                    update(ResearchRun)
                    .where(ResearchRun.id == self.run_id)
                    .values(spent_usd=ResearchRun.spent_usd + cost)
                )
            session.flush()
            return call.id
