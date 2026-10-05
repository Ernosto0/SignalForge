from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import CacheMode, ModelPrice, Settings, get_defaults
from signalforge.db.models import LLMCall, ResearchRun
from signalforge.domain.diagnostics import PainCheck
from signalforge.prompts import Prompt, load_prompt
from signalforge.providers.cache import CacheMiss, CacheStore
from signalforge.providers.llm import (
    BudgetExceeded,
    Completion,
    LLMError,
    LLMService,
    ModelTier,
    compute_cost,
)

PROMPT = load_prompt("llm_check")
TEXT = "Sevkiyatları Excel'de takip ediyoruz."
ANSWER = {
    "is_business_pain": True,
    "signal_type": "workaround",
    "actor": "logistics company",
    "summary_en": "They track shipments in Excel.",
}


class FakeClient:
    def __init__(self, parsed: dict | None = ANSWER) -> None:
        self.parsed = parsed
        self.calls: list[dict] = []

    def parse(self, **kwargs) -> Completion:
        self.calls.append(kwargs)
        return Completion(
            parsed=self.parsed,
            input_tokens=1_000,
            cached_tokens=200,
            output_tokens=100,
            response_id="resp_1",
            error=None if self.parsed else "refused",
        )


def _service(
    db: sessionmaker[Session],
    client: FakeClient,
    mode: CacheMode = CacheMode.LIVE,
    run_id: int | None = None,
) -> LLMService:
    settings = Settings(_env_file=None, llm_model_fast="gpt-6-luna", llm_model_analysis="mystery")
    return LLMService(
        client,
        CacheStore(db, mode, {"llm": None}),
        db,
        settings,
        get_defaults().llm.pricing,
        max_output_tokens=500,
        run_id=run_id,
    )


def _calls(db: sessionmaker[Session]) -> list[LLMCall]:
    with db() as session:
        return list(session.scalars(select(LLMCall).order_by(LLMCall.id)))


def _run(db: sessionmaker[Session], budget: str, spent: str = "0") -> int:
    with db.begin() as session:
        run = ResearchRun(
            request={},
            pack_id="tr",
            pack_version="0.1.0",
            config_hash="x",
            budget_usd=Decimal(budget),
            spent_usd=Decimal(spent),
        )
        session.add(run)
        session.flush()
        return run.id


def test_compute_cost_uses_cached_input_rate() -> None:
    price = ModelPrice(input=Decimal("2"), cached_input=Decimal("0.2"), output=Decimal("10"))
    # 800 fresh * $2 + 200 cached * $0.2 + 100 out * $10, per million tokens
    assert compute_cost(price, 1_000, 200, 100) == Decimal("0.002640")


def test_structured_call_is_parsed_and_logged_with_cost(db) -> None:
    client = FakeClient()
    result = _service(db, client).parse(ModelTier.FAST, PROMPT, TEXT, PainCheck, stage="test")

    assert isinstance(result.output, PainCheck) and result.output.signal_type == "workaround"
    assert client.calls[0]["model"] == "gpt-6-luna"
    assert client.calls[0]["schema"] is PainCheck
    assert client.calls[0]["instructions"] == PROMPT.text
    [call] = _calls(db)
    assert call.id == result.call_id
    assert (call.stage, call.tier, call.model) == ("test", "fast", "gpt-6-luna")
    assert (call.prompt_id, call.prompt_version) == ("llm_check", PROMPT.version)
    assert (call.input_tokens, call.cached_tokens, call.output_tokens) == (1_000, 200, 100)
    assert call.cost_usd == result.cost_usd == Decimal("0.000132") and not call.cache_hit


def test_second_identical_call_is_a_free_cache_hit(db) -> None:
    client = FakeClient()
    service = _service(db, client)
    service.parse(ModelTier.FAST, PROMPT, TEXT, PainCheck)
    again = service.parse(ModelTier.FAST, PROMPT, TEXT, PainCheck)

    assert again.cache_hit and again.cost_usd == 0 and len(client.calls) == 1
    assert again.output.summary_en == ANSWER["summary_en"]
    assert [c.cache_hit for c in _calls(db)] == [False, True]


def test_prompt_version_and_text_are_part_of_the_cache_key(db) -> None:
    client = FakeClient()
    service = _service(db, client)
    service.parse(ModelTier.FAST, PROMPT, TEXT, PainCheck)
    service.parse(ModelTier.FAST, PROMPT.model_copy(update={"version": 99}), TEXT, PainCheck)
    service.parse(ModelTier.FAST, Prompt(id="llm_check", version=1, text="edited"), TEXT, PainCheck)
    assert len(client.calls) == 3


def test_replay_serves_recorded_calls_and_fails_on_miss(db) -> None:
    _service(db, FakeClient(), CacheMode.RECORD).parse(ModelTier.FAST, PROMPT, TEXT, PainCheck)

    offline = FakeClient()
    replay = _service(db, offline, CacheMode.REPLAY)
    assert replay.parse(ModelTier.FAST, PROMPT, TEXT, PainCheck).cache_hit
    with pytest.raises(CacheMiss):
        replay.parse(ModelTier.FAST, PROMPT, "başka metin", PainCheck)
    assert offline.calls == []


def test_cost_accumulates_on_run_and_budget_stops_calls(db) -> None:
    run_id = _run(db, budget="0.0002")
    client = FakeClient()
    service = _service(db, client, run_id=run_id)

    service.parse(ModelTier.FAST, PROMPT, "bir", PainCheck)  # $0.000132
    service.parse(ModelTier.FAST, PROMPT, "iki", PainCheck)  # spent < budget: allowed
    with pytest.raises(BudgetExceeded):
        service.parse(ModelTier.FAST, PROMPT, "üç", PainCheck)
    # Cache hits are free and still allowed after the budget is spent.
    assert service.parse(ModelTier.FAST, PROMPT, "bir", PainCheck).cache_hit

    with db() as session:
        assert session.get(ResearchRun, run_id).spent_usd == Decimal("0.000264")
    assert len(client.calls) == 2


def test_refusal_is_logged_with_cost_and_not_cached(db) -> None:
    client = FakeClient(parsed=None)
    service = _service(db, client)
    for _ in range(2):
        with pytest.raises(LLMError, match="refused"):
            service.parse(ModelTier.FAST, PROMPT, TEXT, PainCheck)

    calls = _calls(db)
    assert len(client.calls) == 2
    assert all(c.error == "refused" and c.cost_usd > 0 for c in calls)


def test_client_exception_is_logged(db) -> None:
    class Broken(FakeClient):
        def parse(self, **kwargs) -> Completion:
            raise TimeoutError("slow")

    with pytest.raises(TimeoutError):
        _service(db, Broken()).parse(ModelTier.FAST, PROMPT, TEXT, PainCheck)
    [call] = _calls(db)
    assert call.error == "TimeoutError: slow" and call.cost_usd == 0


def test_model_without_pricing_fails_before_calling(db) -> None:
    class Anything(BaseModel):
        x: int

    client = FakeClient()
    with pytest.raises(LLMError, match="no pricing"):
        _service(db, client).parse(ModelTier.ANALYSIS, PROMPT, TEXT, Anything)
    assert client.calls == []
