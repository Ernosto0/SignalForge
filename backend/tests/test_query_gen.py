import hashlib
import json
from collections import Counter
from decimal import Decimal

import httpx
import pytest
from openai import OpenAI
from sqlalchemy import select as sql_select
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import CacheMode, Settings, get_defaults
from signalforge.db.models import Query, ResearchRun, StageRun
from signalforge.domain.plan import PlanSubmarket, ResearchPlan
from signalforge.domain.queries import QueryDraft, QueryDrafts
from signalforge.packs import load_pack
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import create_run, run_stage
from signalforge.pipeline.stages.query_gen import (
    Candidate,
    Deduper,
    QueryGen,
    _interleave,
    allocate,
    clean_query,
    generate,
    select,
)
from signalforge.providers.cache import CacheMiss, CacheStore
from signalforge.providers.llm import Completion, LLMError, LLMService, OpenAIClient
from signalforge.text import match_tokens, tr_casefold

PACK = load_pack("tr")
CFG = get_defaults().query_gen
HINTS = {"pain": "sikayetvar.com", "jobs": "kariyer.net", "regulatory": "gib.gov.tr"}


def _plan(n_submarkets: int = 2) -> ResearchPlan:
    return ResearchPlan.model_validate(
        {
            "request": {
                "country": "TR",
                "industry": "logistics",
                "target_customer": "SMB",
                "business_model": "B2B SaaS",
                "founder": {
                    "team": "solo",
                    "budget": "low",
                    "mvp_months": 3,
                    "min_customer_value_usd_month": 50,
                },
                "budget_usd": 1,
            },
            "submarkets": [
                {"name": f"Sub {i}", "name_tr": f"Alt pazar {i}"} for i in range(n_submarkets)
            ],
        }
    )


def _words(*seed: object) -> str:
    """Two pseudo-random tokens: distinct seeds never look like near-duplicates."""
    digest = hashlib.sha256(repr(seed).encode()).hexdigest()
    return f"{digest[:6]} {digest[6:12]}"


def _drafts_for(prompt_input: str) -> QueryDrafts:
    """What a well-behaved model returns: exactly the requested counts and hint shares."""
    task = json.loads(prompt_input.split("# Submarket\n", 1)[1])
    name = task["submarket"]["name"]
    drafts = []
    for intent, ask in task["counts"].items():
        types = (
            ask.get("by_signal_type")
            or {
                "jobs": {"labor_spend": ask["total"]},
                "regulatory": {"regulatory": ask["total"]},
            }[intent]
        )
        i = 0
        for signal_type, n in types.items():
            for _ in range(n):
                hint = HINTS[intent] if i < ask["with_source_hint"] else None
                drafts.append(QueryDraft(text=_words(name, intent, i), intent=intent,
                                         signal_type=signal_type, source_hint=hint))  # fmt: skip
                i += 1
    return QueryDrafts(queries=drafts)


def _fake_draft(sub: PlanSubmarket, prompt_input: str) -> tuple[QueryDrafts, bool]:
    return _drafts_for(prompt_input), False


# --- text -----------------------------------------------------------------------------------


def test_turkish_casefold_treats_dotted_and_dotless_i_as_one_class() -> None:
    assert tr_casefold("İRSALİYE") == tr_casefold("irsaliye") == "irsaliye"
    assert tr_casefold("IŞIK") == tr_casefold("ışık") == tr_casefold("işik")
    assert match_tokens('"E-İrsaliye" Zorunluluğu!') == ["e", "irsaliye", "zorunluluğu"]


# --- pure steps -----------------------------------------------------------------------------


def test_allocate_is_proportional_and_exact() -> None:
    assert allocate(10, {"a": 0.65, "b": 0.2, "c": 0.15}) == {"a": 7, "b": 2, "c": 1}
    assert sum(allocate(143, dict.fromkeys("abcdef", 1.0)).values()) == 143
    assert allocate(5, {"a": 0.0}) == {"a": 0}


@pytest.mark.parametrize(
    ("text", "hint", "expected", "note"),
    [
        ("nakliye  irsaliye  forum", None, ("nakliye irsaliye forum", None), None),
        ("Mobiliz şikayet site:sikayetvar.com", None, ("Mobiliz şikayet", "sikayetvar.com"),
         "fixed_site_operator_in_text"),
        ("Mobiliz şikayet", "https://www.sikayetvar.com/mobiliz", ("Mobiliz şikayet",
         "sikayetvar.com"), None),
        ("forum konusu var", "forum.donanimhaber.com", ("forum konusu var",
         "forum.donanimhaber.com"), None),
        ("nakliye programı şikayet", "kariyer.net", ("nakliye programı şikayet", None),
         "fixed_unoffered_source_hint"),
        ("nakliye programı şikayet", "uydurma.com", ("nakliye programı şikayet", None),
         "fixed_unoffered_source_hint"),
        ("nakliye -evden şikayet", None, None, "dropped_operator"),
        ("nakliye OR kargo şikayet", None, None, "dropped_operator"),
        ("intitle:nakliye şikayet", None, None, "dropped_operator"),
        ("e-İrsaliye U-ETDS zorunluluğu", None, ("e-İrsaliye U-ETDS zorunluluğu", None), None),
        ('“stok sayım farkı” depo', None, ('"stok sayım farkı" depo', None), None),
        ('"stok farkı depo', None, ("stok farkı depo", None), "fixed_unbalanced_quotes"),
        ('"stok farkı" "depo sayımı" forum', None, ('"stok farkı" depo sayımı forum', None),
         "fixed_extra_quotes"),
        ("nakliye", None, None, "dropped_length"),
        (" ".join(["kelime"] * 11), None, None, "dropped_length"),
    ],
)  # fmt: skip
def test_clean_query(text, hint, expected, note) -> None:
    notes: Counter[str] = Counter()
    offered = ["sikayetvar.com", "donanimhaber.com"]
    assert clean_query(text, hint, offered, CFG, notes) == expected
    assert list(notes) == ([note] if note else [])


def test_deduper_ignores_case_turkish_i_quotes_and_word_order_but_not_source_hint() -> None:
    dedupe = Deduper(90)
    assert dedupe.add("e-İrsaliye zorunluluğu nakliye", None)
    assert not dedupe.add('"E-irsaliye" ZORUNLULUĞU nakliye', None)
    assert not dedupe.add("nakliye e-irsaliye zorunluluğu", None)
    assert not dedupe.add("e-İrsaliye zorunlulugu nakliye", None)  # near-identical spelling
    assert dedupe.add("e-İrsaliye zorunluluğu nakliye", "gib.gov.tr")
    assert dedupe.add("e-İrsaliye geçiş tarihi depo", None)


def _cand(signal_type: str, i: int, submarket: str = "S", intent: str = "pain") -> Candidate:
    return Candidate(f"q{signal_type}{i}", intent, signal_type, None, submarket, "llm")


def test_interleave_follows_weights_in_every_prefix() -> None:
    items = [_cand("a", i) for i in range(10)] + [_cand("b", i) for i in range(10)]
    ordered = list(_interleave(items, {"a": 0.75, "b": 0.25}))
    assert [c.signal_type for c in ordered[:8]].count("a") == 6
    assert [c.text for c in ordered if c.signal_type == "a"] == [f"qa{i}" for i in range(10)]
    # Without weights the types alternate.
    assert [c.signal_type for c in _interleave(items, {})][:4] == ["a", "b", "a", "b"]


def test_select_fills_quotas_then_spare_capacity_and_respects_cap() -> None:
    short = [_cand("x", i, "A") for i in range(2)]  # quota 5, only 2 available
    rich = [_cand("x", i, "B") for i in range(10)]
    quotas = {("A", "pain"): 5, ("B", "pain"): 5}
    chosen = select(short + rich, quotas, cap=10)
    assert Counter(c.submarket for c in chosen) == {"A": 2, "B": 8}
    assert len(select(short + rich, quotas, cap=4)) == 4


# --- generate -------------------------------------------------------------------------------


def test_generate_meets_cap_quotas_and_reports_metrics() -> None:
    plan = _plan(3)
    generated = generate(plan, PACK, CFG, _fake_draft)
    queries, m = generated.queries, generated.metrics

    seeds = [s for mandate in PACK.regulatory.mandates for s in mandate.seeds]
    assert len(queries) == m["kept"] == CFG.max_queries
    assert [q.text for q in queries if q.origin == "pack_seed"] == seeds
    assert m["pack_seeds"] == len(seeds) and m["llm_calls"] == 3
    assert m["quota_shortfall"] == {}
    per_sub = Counter(q.submarket for q in queries if q.submarket)
    assert max(per_sub.values()) - min(per_sub.values()) <= 1
    by_intent = m["kept_by_intent"]
    assert by_intent["pain"] > by_intent["jobs"] > by_intent["regulatory"] - len(seeds)
    assert all(q.signal_type == "labor_spend" for q in queries if q.intent == "jobs")
    assert all(q.query.endswith(" site:kariyer.net") for q in queries
               if q.intent == "jobs" and q.source_hint)  # fmt: skip
    assert len({q.query for q in queries}) == len(queries)
    # Pain follows the configured signal-type mix (±1 per type and submarket).
    pain = Counter(q.signal_type for q in queries if q.intent == "pain" and q.submarket == "Sub 0")
    quota = sum(pain.values())
    for signal_type, weight in CFG.pain_signal_mix.items():
        assert abs(pain[signal_type] - quota * weight) <= 1


def test_generate_prompt_input_has_shared_prefix_and_per_submarket_counts() -> None:
    inputs: list[str] = []

    def draft(sub: PlanSubmarket, prompt_input: str) -> tuple[QueryDrafts, bool]:
        inputs.append(prompt_input)
        return QueryDrafts(queries=[]), True

    m = generate(_plan(2), PACK, CFG, draft).metrics
    context_a, task_a = inputs[0].split("# Submarket\n")
    context_b, _ = inputs[1].split("# Submarket\n")
    assert context_a == context_b  # cacheable prefix
    assert "sikayetvar.com" in context_a and "Excel'de takip ediyoruz" in context_a
    counts = json.loads(task_a)["counts"]
    assert set(counts) == {"pain", "jobs", "regulatory"}
    assert sum(counts["pain"]["by_signal_type"].values()) == counts["pain"]["total"]
    # The model returned nothing: only seeds survive, and the shortfall is reported.
    assert m["llm_cache_hits"] == 2 and m["kept"] == m["pack_seeds"]
    assert m["quota_shortfall"]["Sub 0/pain"] > 0


def test_generate_drops_duplicates_across_submarkets() -> None:
    def draft(sub: PlanSubmarket, prompt_input: str) -> tuple[QueryDrafts, bool]:
        same = QueryDraft(text="gümrük beyannamesi hata forum", intent="pain",
                          signal_type="complaint", source_hint=None)  # fmt: skip
        return QueryDrafts(queries=[same]), False

    generated = generate(_plan(2), PACK, CFG, draft)
    assert sum(q.origin == "llm" for q in generated.queries) == 1
    assert generated.metrics["dropped_duplicate"] == 1


# --- stage (DB) -----------------------------------------------------------------------------


class FakeLLMClient:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def parse(self, *, input: str, max_output_tokens: int, **kwargs) -> Completion:
        self.calls += 1
        assert max_output_tokens == CFG.max_output_tokens
        parsed = None if self.fail else _drafts_for(input).model_dump(mode="json")
        return Completion(parsed=parsed, input_tokens=3000, cached_tokens=0, output_tokens=2000,
                          error="truncated" if self.fail else None)  # fmt: skip


def _context(
    db: sessionmaker[Session], run_id: int, client: FakeLLMClient, mode=CacheMode.LIVE
) -> RunContext:
    settings = Settings(_env_file=None, llm_model_fast="gpt-6-luna")
    defaults = get_defaults()
    cache = CacheStore(db, mode, {"llm": None})
    llm = LLMService(client, cache, db, settings, defaults.llm.pricing, 4000, run_id=run_id)
    return RunContext(settings=settings, defaults=defaults, db=db, cache=cache, pack=PACK,
                      search=None, fetcher=None, llm=llm, run_id=run_id)  # fmt: skip


def _queries(db: sessionmaker[Session], run_id: int) -> list[Query]:
    with db() as session:
        return list(session.scalars(sql_select(Query).where(Query.run_id == run_id)))


def test_stage_stores_queries_records_stage_run_and_is_idempotent(db) -> None:
    run_id = create_run(db, _plan(2), PACK, get_defaults())
    client = FakeLLMClient()
    stage = run_stage(_context(db, run_id, client), QueryGen())

    stored = _queries(db, run_id)
    assert stage.status == "completed" and stage.input_hash
    assert stage.metrics["kept"] == len(stored) == CFG.max_queries
    # 2 calls × (3000 in × $0.10 + 2000 out × $0.50) per MTok
    assert stage.cost_usd == Decimal("0.0026") and client.calls == 2
    jobs = [q for q in stored if q.intent == "jobs"]
    assert all(q.meta["signal_type"] == "labor_spend" and q.lang == "tr" for q in jobs)
    hinted = [q for q in jobs if q.meta["source_hint"]]
    assert hinted and all(q.text.endswith(" site:kariyer.net") for q in hinted)
    assert {q.meta["origin"] for q in stored} == {"llm", "pack_seed"}

    # Rerun: replaces the stage's queries; drafts come from the LLM cache for free.
    again = run_stage(_context(db, run_id, client), QueryGen())
    assert sorted(q.text for q in _queries(db, run_id)) == sorted(q.text for q in stored)
    assert again.cost_usd == 0 and again.metrics["llm_cache_hits"] == 2 and client.calls == 2

    # Replay mode serves the recorded drafts; a changed plan would be a miss.
    offline = FakeLLMClient()
    run_stage(_context(db, run_id, offline, CacheMode.REPLAY), QueryGen())
    assert offline.calls == 0
    other = create_run(db, _plan(3), PACK, get_defaults())
    with pytest.raises(CacheMiss):
        run_stage(_context(db, other, offline, CacheMode.REPLAY), QueryGen())


def test_stage_failure_is_recorded_with_cost(db) -> None:
    run_id = create_run(db, _plan(1), PACK, get_defaults())
    with pytest.raises(LLMError, match="truncated"):
        run_stage(_context(db, run_id, FakeLLMClient(fail=True)), QueryGen())
    with db() as session:
        stage = session.scalars(sql_select(StageRun).where(StageRun.run_id == run_id)).one()
        run = session.get(ResearchRun, run_id)
    assert stage.status == "failed" and "truncated" in stage.error
    assert stage.cost_usd == Decimal("0.0013") and run.status == "failed"
    assert _queries(db, run_id) == []

    # A successful re-run of the stage leaves the run resumable, not failed.
    assert run_stage(_context(db, run_id, FakeLLMClient()), QueryGen()).status == "completed"
    with db() as session:
        assert session.get(ResearchRun, run_id).status == "stopped"


# --- provider: truncated structured output --------------------------------------------------


def test_openai_client_reports_truncated_output_with_its_usage() -> None:
    body = {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6-luna",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "incomplete",
                "content": [
                    {"type": "output_text", "text": '{"queries":[{"text":"nak', "annotations": []}
                ],  # fmt: skip
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 3100,
            "input_tokens_details": {"cached_tokens": 1024},
            "output_tokens": 4000,
            "output_tokens_details": {"reasoning_tokens": 3500},
            "total_tokens": 7100,
        },
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    sdk = OpenAI(api_key="test", http_client=httpx.Client(transport=transport))
    completion = OpenAIClient("test", client=sdk).parse(
        model="gpt-6-luna", instructions="x", input="y", schema=QueryDrafts, max_output_tokens=4000
    )
    assert completion.parsed is None
    assert (completion.input_tokens, completion.cached_tokens, completion.output_tokens) == (
        3100,
        1024,
        4000,
    )
    assert "max_output_tokens" in completion.error and completion.response_id == "resp_1"
