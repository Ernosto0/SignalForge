"""problem_discovery agent (agent-modules.md §3): extract → cluster.

Quote matching itself (casefold, fuzzy, invented / stitched quotes) is tested in test_evidence.py;
here it is exercised through extraction: offsets across chunks, dropped quotes, author hashing.
"""

import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from fakes import OTHER, PACK, PARAGRAPH, PLAN, FakeLLM, FakeSearch, make_context
from pydantic import BaseModel
from sqlalchemy import func, select

from signalforge.agents import AGENTS, STAGES, get_agent
from signalforge.agents.problem_discovery import PROBLEM_DISCOVERY
from signalforge.config import get_defaults
from signalforge.db.models import (
    Claim,
    Excerpt,
    IndependenceGroup,
    ProblemCluster,
    Query,
    Signal,
)
from signalforge.domain.evidence import (
    AssignmentBatch,
    ClusterAssignment,
    ClusterBatch,
    ClusterDraft,
    ClusterMerge,
    ExtractedSignal,
    ExtractionBatch,
    MergeBatch,
)
from signalforge.evidence.extraction import SourceText, chunk_text, extract_document
from signalforge.pipeline.runner import create_run, latest_stage_runs, run_pipeline
from signalforge.pipeline.stages.cluster import SignalItem, cluster_signals
from signalforge.pipeline.stages.extract import extract
from signalforge.providers.llm import LLMError

DEFAULTS = get_defaults()
EXTRACT = DEFAULTS.extract
CLUSTER = DEFAULTS.cluster
ROAD, CUSTOMS = "Road freight", "Customs brokerage"


def _signal(quote: str, author: str | None = None, **kw: Any) -> ExtractedSignal:
    fields = {"translation": "t", "type": "complaint", "actor": "nakliyeci", "workflow": "w",
              "statement": f"statement for {quote[:20]}", "first_hand": True, "author": author,
              **kw}  # fmt: skip
    return ExtractedSignal(quote=quote, **fields)


def _source(doc_id: int, text: str, **kw: Any) -> SourceText:
    fields = {"domain": f"d{doc_id}.com", "title": "t", "source_category": "forum",
              "quality_tier": "medium", "triage_label": "first_hand_pain", "source": "text",
              **kw}  # fmt: skip
    return SourceText(document_id=doc_id, text=text, **fields)


# --- agents ---------------------------------------------------------------------------------


def test_stage_order_is_derived_from_the_agents() -> None:
    assert [s.name for s in STAGES] == [s.name for a in AGENTS for s in a.stages]
    assert len({s.name for s in STAGES}) == len(STAGES)
    agent = get_agent("problem_discovery")
    assert agent is PROBLEM_DISCOVERY
    assert (agent.first, agent.last) == ("extract", "cluster")
    with pytest.raises(ValueError, match="unknown agent"):
        get_agent("nope")


# --- extract --------------------------------------------------------------------------------


def test_chunk_text_cuts_at_boundaries_with_overlap_and_cap() -> None:
    cfg = EXTRACT.model_copy(
        update={"chunk_chars": 500, "chunk_overlap_chars": 50, "max_chunks_per_doc": 3}
    )
    assert chunk_text("kısa metin", cfg) == [(0, "kısa metin")]

    text = "\n".join(f"Satır {i}: " + "irsaliye takibi elle yapılıyor. " * 3 for i in range(40))
    chunks = chunk_text(text, cfg)
    assert len(chunks) == 3
    for offset, chunk in chunks:
        assert text[offset : offset + len(chunk)] == chunk
        assert len(chunk) <= 500
    assert chunks[0][1].endswith("\n")  # cut after a line break
    assert chunks[1][0] < chunks[0][0] + len(chunks[0][1])  # overlap


def test_extract_keeps_only_verified_quotes_and_groups_authors() -> None:
    sources = [
        _source(1, PARAGRAPH),
        _source(2, OTHER),
        _source(3, "Kısa ilan metni: operasyon ekibi irsaliye girişi yapılacak.", source="snippet"),
    ]
    first_sentence = PARAGRAPH.split(". ")[0] + "."

    def propose(prompt_input: str) -> tuple[ExtractionBatch, bool]:
        text = prompt_input.split("# Text\n", 1)[1]
        if text.startswith("Gümrük"):
            return ExtractionBatch(
                signals=[
                    _signal(OTHER.split(", eksik")[0], author="Müşavir Ayşe", submarket=CUSTOMS),
                    _signal("Gümrükte her şey otomatik ve sorunsuz ilerliyor."),  # invented
                ]
            ), True
        if text.startswith("Kısa"):
            raise LLMError("output cut off")
        return (
            ExtractionBatch(
                signals=[
                    # same author as doc 2; submarket named by its local name, in capitals
                    _signal(
                        first_sentence.upper(),
                        author="müşavir ayşe",
                        submarket="KARAYOLU YÜK TAŞIMACILIĞI / NAKLİYE",
                    ),  # fmt: skip
                    _signal(first_sentence[:40]),  # overlaps the first one
                    _signal("Excel"),  # verified but too short
                    _signal("Şoförlerimiz her gün faturaları kaybediyor."),  # invented
                ]
            ),
            False,
        )

    result = extract(sources, PLAN, PACK, EXTRACT, propose, salt="s")

    assert [(k.document_id, k.match.verified, k.submarket) for k in result.signals] == [
        (1, "exact", ROAD),
        (2, "exact", CUSTOMS),
    ]
    assert result.signals[0].match.quote == first_sentence  # source text, not the model's copy
    assert result.signals[0].author_hash == result.signals[1].author_hash
    assert [(g.rule, g.document_ids) for g in result.author_groups] == [("same_author", [1, 2])]
    m = result.metrics
    assert (m["quotes_proposed"], m["quotes_unverified"], m["quote_pass_rate"]) == (6, 2, 0.667)
    assert (m["dropped_length"], m["dropped_overlap"], m["llm_errors"]) == (1, 1, 1)
    assert (m["llm_calls"], m["llm_cache_hits"], m["signals"]) == (3, 1, 2)
    assert m["yield_by_category"] == {"forum": {"documents": 3, "with_signals": 2, "signals": 2}}
    assert m["signals_by_submarket"] == {ROAD: 1, CUSTOMS: 1}


def test_offsets_in_later_chunks_point_into_the_whole_text() -> None:
    cfg = EXTRACT.model_copy(update={"chunk_chars": 400, "chunk_overlap_chars": 40})
    text = PARAGRAPH + "\n" + OTHER + "\n" + PARAGRAPH
    quote = OTHER.split(", eksik")[0]

    def propose(prompt_input: str) -> tuple[ExtractionBatch, bool]:
        chunk = prompt_input.split("# Text\n", 1)[1]
        found = quote in chunk
        return ExtractionBatch(signals=[_signal(quote, submarket="Uzay")] if found else []), False

    result = extract_document(_source(1, text), PLAN, PACK, cfg, propose, salt="s")

    (kept,) = result.signals  # also seen in the chunk overlap, kept once
    assert kept.match.char_start > 0
    assert text[kept.match.char_start : kept.match.char_end] == quote
    assert kept.submarket is None  # not one of the plan's submarkets
    assert result.notes["submarket_unmatched"] >= 1


# --- cluster --------------------------------------------------------------------------------


def _items(n: int, **kw: Any) -> list[SignalItem]:
    fields = {"type": "complaint", "actor": "a", "workflow": "w", "first_hand": True, **kw}
    return [SignalItem(i, statement=f"s{i}", document_id=i, **fields) for i in range(1, n + 1)]


def _ids(prompt_input: str) -> list[int]:
    return [s["id"] for s in json.loads(prompt_input.split("# Signals\n", 1)[1])]


class FakeAsk:
    """Answers the cluster prompts with the given functions and records which were asked."""

    def __init__(self, **answers: Callable[[str], BaseModel]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.inputs: list[str] = []

    def __call__(self, prompt_id: str, prompt_input: str, schema: type) -> tuple[Any, bool]:
        self.asked.append(prompt_id)
        self.inputs.append(prompt_input)
        out = self.answers[prompt_id](prompt_input)
        assert isinstance(out, schema)
        return out, False


def test_cluster_validation_makes_every_signal_appear_once() -> None:
    def cluster(_: str) -> ClusterBatch:
        return ClusterBatch(
            clusters=[
                ClusterDraft(name="A", description="a", signal_ids=[1, 2, 3, 99]),  # 99 unknown
                ClusterDraft(name="B", description="b", signal_ids=[3, 4, 5]),  # 3 repeated
                ClusterDraft(name="C", description="c", signal_ids=[6]),  # too small
            ],
            noise=[7, 7],
        )  # 8, 9 missing

    def assign(prompt_input: str) -> AssignmentBatch:
        assert _ids(prompt_input) == [8, 9]
        return AssignmentBatch(
            assignments=[
                ClusterAssignment(signal_id=8, cluster=1),
                ClusterAssignment(signal_id=9, cluster=7),  # no such cluster -> noise
            ]
        )

    ask = FakeAsk(cluster=cluster, cluster_assign=assign)
    result = cluster_signals(_items(9), PLAN, PACK, CLUSTER, ask)

    assert [(d.name, d.signal_ids) for d in result.clusters] == [
        ("A", [1, 2, 3]),
        ("B", [4, 5, 8]),
    ]
    assert result.noise == [6, 7, 9]
    n = result.notes
    assert (n["unknown_ids"], n["repeated_ids"], n["missing_ids"]) == (1, 2, 2)
    assert (n["reassigned"], n["leftover_noise"], n["too_small_ids"]) == (1, 1, 1)
    assert ask.asked == ["cluster", "cluster_assign"]


def test_repeated_id_stays_with_signals_from_its_own_document() -> None:
    # Signals 3 and 5 come from the same document; 3 is put in both clusters.
    items = [
        SignalItem(i, "complaint", "a", "w", f"s{i}", document_id=doc, first_hand=True)
        for i, doc in [(1, 1), (2, 2), (3, 9), (4, 4), (5, 9)]
    ]

    def cluster(_: str) -> ClusterBatch:
        return ClusterBatch(
            clusters=[
                ClusterDraft(name="A", description="a", signal_ids=[1, 2, 3]),
                ClusterDraft(name="B", description="b", signal_ids=[3, 4, 5]),
            ],
            noise=[],
        )

    result = cluster_signals(items, PLAN, PACK, CLUSTER, FakeAsk(cluster=cluster))
    assert [(d.name, d.signal_ids) for d in result.clusters] == [("B", [3, 4, 5]), ("A", [1, 2])]
    assert result.notes["repeated_ids"] == 1


@pytest.mark.parametrize("keep", [True, False])
def test_a_lone_regulatory_signal_may_form_a_cluster(keep: bool) -> None:
    cfg = CLUSTER.model_copy(update={"keep_regulatory_singletons": keep})
    items = _items(3)
    items[2] = SignalItem(3, "regulatory", None, "w", "s3", document_id=3, first_hand=False)

    def cluster(_: str) -> ClusterBatch:
        return ClusterBatch(
            clusters=[
                ClusterDraft(name="A", description="a", signal_ids=[1, 2]),
                ClusterDraft(name="Mandate", description="m", signal_ids=[3]),
            ],
            noise=[],
        )

    result = cluster_signals(items, PLAN, PACK, cfg, FakeAsk(cluster=cluster))
    names = [d.name for d in result.clusters]
    assert names == (["A", "Mandate"] if keep else ["A"])
    assert result.noise == ([] if keep else [3])


def test_clusters_over_the_cap_are_dissolved_and_reassigned() -> None:
    cfg = CLUSTER.model_copy(update={"max_clusters": 2})

    def cluster(_: str) -> ClusterBatch:
        return ClusterBatch(
            clusters=[
                ClusterDraft(name="small", description="", signal_ids=[5, 6]),
                ClusterDraft(name="big", description="", signal_ids=[1, 2, 3]),
                ClusterDraft(name="mid", description="", signal_ids=[4, 7]),
            ],
            noise=[],
        )

    def assign(prompt_input: str) -> AssignmentBatch:
        return AssignmentBatch(
            assignments=[ClusterAssignment(signal_id=i, cluster=0) for i in _ids(prompt_input)]
        )

    ask = FakeAsk(cluster=cluster, cluster_assign=assign)
    result = cluster_signals(_items(7), PLAN, PACK, cfg, ask)
    # Largest first, model order among equal sizes: "mid" is the one over the cap.
    assert [(d.name, d.signal_ids) for d in result.clusters] == [
        ("big", [1, 2, 3, 4, 7]),
        ("small", [5, 6]),
    ]
    assert result.notes["overflow_ids"] == 2
    assert result.noise == []


def test_large_runs_are_clustered_in_parts_by_submarket_and_merged() -> None:
    cfg = CLUSTER.model_copy(update={"max_signals_per_call": 5})
    items = _items(9)
    for i in (1, 3, 5, 7):  # odd ids are road freight, so they share a part
        items[i - 1] = SignalItem(i, "complaint", "a", "w", f"s{i}", i, True, submarket=ROAD)

    def cluster(prompt_input: str) -> ClusterBatch:
        ids = _ids(prompt_input)
        return ClusterBatch(
            clusters=[ClusterDraft(name=f"part{ids[0]}", description="", signal_ids=ids[:-1])],
            noise=ids[-1:],
        )

    def merge(prompt_input: str) -> MergeBatch:
        listing = json.loads(prompt_input.split("# Clusters\n", 1)[1])
        assert [c["size"] for c in listing] == [4, 3]  # 9 signals -> parts of 5 and 4
        return MergeBatch(clusters=[ClusterMerge(name="merged", description="", members=[0, 1])])

    ask = FakeAsk(cluster=cluster, cluster_merge=merge)
    result = cluster_signals(items, PLAN, PACK, cfg, ask)
    assert [_ids(i) for i in ask.inputs[:2]] == [[1, 3, 5, 7, 2], [4, 6, 8, 9]]
    assert [(d.name, d.signal_ids) for d in result.clusters] == [("merged", [1, 3, 4, 5, 6, 7, 8])]
    assert result.noise == [2, 9]
    assert ask.asked == ["cluster", "cluster", "cluster_merge"]


# --- pipeline (DB) ---------------------------------------------------------------------------

THIRD = (
    "Depo sorumlusu olarak stok sayımlarını her ay kağıt listelerle yapıyoruz ve farkları bulmak "
    "için günlerce kayıtları karşılaştırıyoruz. Sistemimiz el terminalini desteklemiyor. "
)
FOURTH = (
    "Filo yöneticisiyim; takograf verilerini her hafta araçlardan tek tek indirip ayrı bir "
    "programa yüklüyoruz, U-ETDS bildirimlerini de elle giriyoruz. Çok vakit alıyor. "
)
AUTHOR = "Lojistikçi Mehmet"


def _page_texts() -> Callable[[str], str]:
    """A different text per page path (in request order), so documents are independent."""
    texts = [PARAGRAPH * 2, OTHER * 3, THIRD * 2, FOURTH * 2]
    assigned: dict[str, str] = {}

    def page_text(path: str) -> str:
        if path not in assigned:
            assigned[path] = texts[len(assigned) % len(texts)]
        return assigned[path]

    return page_text


def _extract_answer(prompt_input: str) -> ExtractionBatch:
    text = prompt_input.split("# Text\n", 1)[1]
    first = re.split(r"(?<=\.)\s", text, maxsplit=1)[0]  # first sentence
    return ExtractionBatch(
        signals=[
            _signal(first, author=AUTHOR if text.startswith("Nakliye") else None, submarket=ROAD),
            _signal("Bu cümle sayfada hiç geçmiyor ama model uydurdu."),
        ]
    )


def _cluster_answer(prompt_input: str) -> ClusterBatch:
    ids = _ids(prompt_input)
    return ClusterBatch(
        clusters=[ClusterDraft(name="Manual data re-entry", description="d", signal_ids=ids)],
        noise=[],
    )


def test_problem_discovery_writes_signals_claims_and_clusters(db, monkeypatch) -> None:
    monkeypatch.setattr("signalforge.pipeline.stages.search.time.sleep", lambda _: None)
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    with db.begin() as session:
        session.add_all(
            Query(run_id=run_id, text=t, lang="tr", intent="pain", meta={})
            for t in ["irsaliye excel", "gümrük evrak", "depo sayım farkı"]
        )
    llm = FakeLLM({ExtractionBatch: _extract_answer, ClusterBatch: _cluster_answer})
    ctx = make_context(db, run_id, FakeSearch(), llm, page_text=_page_texts())
    stages = STAGES[1:]  # query_gen needs the real model; queries were inserted above
    run_pipeline(ctx, stages)

    latest = latest_stage_runs(db, run_id)
    assert all(latest[s.name].status == "completed" for s in stages)
    m = {name: row.metrics for name, row in latest.items()}
    assert m["extract"]["documents_read"] == 4
    assert (m["extract"]["signals"], m["extract"]["quote_pass_rate"]) == (4, 0.5)
    assert m["extract"]["signals_by_submarket"] == {ROAD: 4}
    assert (m["cluster"]["clusters"], m["cluster"]["noise"]) == (1, 0)
    assert m["cluster"]["clusters_per_submarket"] == {ROAD: 1}
    assert m["cluster"]["clusters_without_facts"] == 0

    def snapshot() -> dict[str, Any]:
        with db() as session:
            signals = session.scalars(select(Signal).order_by(Signal.id)).all()
            excerpts = {e.id: e for e in session.scalars(select(Excerpt))}
            facts = session.scalars(select(Claim).where(Claim.kind == "fact")).all()
            (cluster,) = session.scalars(select(ProblemCluster)).all()
            inference = session.get(Claim, cluster.claim_id)
            return {
                "signals": signals,
                "excerpts": excerpts,
                "facts": facts,
                "cluster": cluster,
                "inference": inference,
                "claims": session.scalar(select(func.count()).select_from(Claim)),
            }

    snap = snapshot()
    signals, excerpts, facts = snap["signals"], snap["excerpts"], snap["facts"]
    assert all(s.meta == {"submarket": ROAD} for s in signals)
    assert all(e.run_id == run_id and e.verified == "exact" for e in excerpts.values())
    # One fact per signal: its statement, supported by exactly its excerpt.
    assert sorted((f.statement, f.supports, f.stage) for f in facts) == sorted(
        (s.statement, [s.excerpt_id], "extract") for s in signals
    )
    # One inference per cluster, derived from the facts of its signals.
    cluster, inference = snap["cluster"], snap["inference"]
    assert cluster.signal_ids == [s.id for s in signals]
    assert (inference.kind, inference.stage, inference.statement) == ("inference", "cluster", "d")
    assert inference.derived_from == sorted(f.id for f in facts)
    assert snap["claims"] == len(facts) + 1
    # The author is only stored as a salted hash.
    hashed = [e.author_hash for e in excerpts.values() if e.author_hash]
    assert len(hashed) == 1 and AUTHOR not in hashed[0]
    with db() as session:
        for e in session.scalars(select(Excerpt)):
            assert all(AUTHOR.casefold() not in str(v).casefold() for v in vars(e).values())

    # Re-running the agent (`run --agent problem_discovery`) replaces its outputs, keeps
    # one claim set and reuses cached LLM answers.
    calls = len(llm.calls)
    agent = get_agent("problem_discovery")
    run_pipeline(ctx, stages, from_stage=agent.first, until=agent.last)
    assert len(llm.calls) == calls
    again = snapshot()
    assert len(again["signals"]) == 4 and again["claims"] == snap["claims"]
    assert again["inference"].derived_from == sorted(f.id for f in again["facts"])
    with db() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(IndependenceGroup)
                .where(IndependenceGroup.rule == "same_author")
            )
            == 0
        )
