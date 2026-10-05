"""Fakes shared by the pipeline tests: search provider, LLM client, page server and RunContext."""

import json
from collections.abc import Callable

import httpx
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from signalforge.agents.loop import LoopAction
from signalforge.config import BACKEND_DIR, CacheMode, Settings, get_defaults
from signalforge.domain.collection import TriageBatch, TriageJudgment
from signalforge.domain.competitors import CompetitorSeeds
from signalforge.domain.plan import load_plan
from signalforge.evidence.entailment import EntailmentBatch, EntailmentVerdict
from signalforge.packs import load_pack
from signalforge.pipeline.context import RunContext
from signalforge.providers.cache import CacheStore
from signalforge.providers.fetch import CachedFetcher, Fetcher
from signalforge.providers.llm import Completion, LLMService
from signalforge.providers.search import CachedSearch, SearchError, SearchHit, SearchLocale

PACK = load_pack("tr")
PLAN = load_plan(BACKEND_DIR / "examples" / "tr-logistics.plan.yaml")

PARAGRAPH = (
    "Nakliye firmamızda sevkiyatları hâlâ Excel tablosunda takip ediyoruz ve şoförler teslimat "
    "bilgisini WhatsApp üzerinden gönderiyor. Her akşam operasyon ekibi bu mesajları tek tek "
    "tabloya giriyor, irsaliye numaralarını elle eşleştiriyor ve müşterilere telefonla dönüyor. "
)
OTHER = (
    "Gümrük müşavirliği bürosunda ithalat dosyalarının evrakları müşterilerden e-posta ile "
    "toplanıyor, eksik belgeler için defalarca aranıyor ve beyanname hazırlığı gecikiyor. "
)


class FakeJudge:
    """Scores from the title (``s<n>``); skips ids listed in ``skip`` on the first call."""

    def __init__(self, skip: set[str] = frozenset()) -> None:
        self.skip = set(skip)
        self.calls = 0

    def __call__(self, prompt_input: str) -> tuple[TriageBatch, bool]:
        self.calls += 1
        items = json.loads(prompt_input.split("# Results\n", 1)[1])
        judgments = []
        for item in items:
            if item["title"] in self.skip:
                self.skip.discard(item["title"])
                continue
            score = int(item["title"].split("s")[-1]) if item["title"].startswith("s") else 0
            judgments.append(
                TriageJudgment(id=item["id"], label="first_hand_pain", score=score, reason="r")
            )
        judgments.append(TriageJudgment(id=999, label="off_topic", score=0, reason="bogus id"))
        return TriageBatch(judgments=judgments), False


class FakeSearch:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.fail_once = {"bad"}

    def search(self, query: str, locale: SearchLocale, n: int) -> list[SearchHit]:
        self.calls += 1
        if any(word in query for word in self.fail_once):
            self.fail_once.clear()
            raise SearchError("timeout")
        slug = abs(hash(query)) % 1000
        return [
            SearchHit(rank=1, url="https://forum.com/shared", title="s3"),
            SearchHit(rank=2, url=f"https://forum.com/t{slug}", title="s3"),
            SearchHit(rank=3, url=f"https://ads.com/{slug}", title="s0"),
        ]


# Answers one structured call: prompt input -> output model.
Handler = Callable[[str], BaseModel]


def support_all(prompt_input: str) -> EntailmentBatch:
    """Entailment answer: every numbered claim is supported."""
    items = json.loads(prompt_input.split("# Claims\n", 1)[1])
    return EntailmentBatch(
        verdicts=[EntailmentVerdict(item=i["item"], verdict="supported", note="ok") for i in items]
    )


class FakeLLM:
    """LLM client answering by output schema. Defaults: triage is judged by :class:`FakeJudge`,
    research loops finish at once, entailment supports every claim, no competitor seeds."""

    def __init__(self, handlers: dict[type[BaseModel], Handler] | None = None) -> None:
        self.handlers: dict[type[BaseModel], Handler] = {
            TriageBatch: lambda prompt_input: FakeJudge()(prompt_input)[0],
            LoopAction: lambda _: LoopAction(action="finish", reason="done"),
            EntailmentBatch: support_all,
            CompetitorSeeds: lambda _: CompetitorSeeds(from_signals=[], suggested=[]),
            **(handlers or {}),
        }
        self.calls: list[type[BaseModel]] = []

    def parse(self, *, input: str, schema: type[BaseModel], **_: object) -> Completion:
        self.calls.append(schema)
        output = self.handlers[schema](input)
        return Completion(output.model_dump(mode="json"), 100, 0, 10)


def same_page(path: str) -> str:
    """Every page carries the same text: one near-dup group."""
    return PARAGRAPH * 3


def make_context(
    db: sessionmaker[Session],
    run_id: int,
    search: FakeSearch,
    llm: FakeLLM | None = None,
    page_text: Callable[[str], str] = same_page,
) -> RunContext:
    defaults = get_defaults()
    cache = CacheStore(db, CacheMode.LIVE, defaults.cache.ttl_days)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        text = page_text(request.url.path)
        body = f"<html lang='tr'><body><article><p>{text}</p></article></body></html>"
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    fetch_cfg = defaults.fetch.model_copy(update={"per_domain_interval_s": 0})
    fetcher = Fetcher(fetch_cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))
    settings = Settings(_env_file=None, author_hash_salt="test-salt")
    service = LLMService(
        llm or FakeLLM(), cache, db, settings, defaults.llm.pricing, 500, run_id=run_id
    )
    return RunContext(
        settings=settings,
        defaults=defaults.model_copy(
            update={"collect": defaults.collect.model_copy(update={"search_retries": 1})}
        ),
        db=db,
        cache=cache,
        pack=PACK,
        search=CachedSearch(search, cache),
        fetcher=CachedFetcher(fetcher, cache),
        llm=service,
        run_id=run_id,
    )
