"""Evidence validator: Gate 1 (shortlist), the problem landscape report (M3), and the verify
stage's second round, entailment and Gate-1 re-check (M4).

The DB tests seed documents, excerpts, signals and clusters directly, so they do not depend on the
extract / cluster stages (problem_discovery).
"""

import json
import re
from datetime import UTC, datetime

from fakes import OTHER, PACK, PARAGRAPH, PLAN, FakeLLM, FakeSearch, make_context
from sqlalchemy import func, select, update

from signalforge.agents.loop import LoopAction
from signalforge.config import get_defaults
from signalforge.db.models import (
    Claim,
    Document,
    Excerpt,
    IndependenceGroup,
    ProblemCluster,
    Query,
    SearchResult,
    Signal,
    StageRun,
)
from signalforge.domain.evidence import VerifyExtractionBatch, VerifySignal
from signalforge.evidence.claims import add_fact, add_inference
from signalforge.evidence.clusters import cluster_signal_ids, current_strength
from signalforge.evidence.entailment import EntailmentBatch, EntailmentVerdict
from signalforge.pipeline.runner import create_run, run_stage
from signalforge.pipeline.stages.cluster import load_evidence
from signalforge.pipeline.stages.shortlist import GateInput, Shortlist, gate1
from signalforge.pipeline.stages.verify import Verify, gate1_trace, regate, write_independence
from signalforge.reporting.landscape import build_landscape, pick_quotes, render_landscape

DEFAULTS = get_defaults()
GATE = DEFAULTS.shortlist.model_copy(
    update={"min_strength": 4.0, "min_independent_sources": 3, "max_shortlisted": 2}
)


# --- gate 1 ---------------------------------------------------------------------------------


def test_gate1_explains_every_decision() -> None:
    decisions = gate1(
        [
            GateInput(1, 6.0, 5),
            GateInput(2, 3.0, 9),  # weak
            GateInput(3, 7.0, 2),  # too few sources
            GateInput(4, 5.0, 4),
            GateInput(5, 4.0, 3),  # passes, but third strongest
        ],
        GATE,
    )
    assert {i for i, d in decisions.items() if d.shortlisted} == {1, 4}
    failed = {i: [t["rule"] for t in d.trace if not t["passed"]] for i, d in decisions.items()}
    assert failed == {
        1: [], 2: ["min_strength"], 3: ["min_independent_sources"], 4: [],
        5: ["max_shortlisted"],
    }  # fmt: skip
    assert decisions[5].trace[-1]["value"] == 3  # its position among passing clusters


def test_gate1_ties_break_by_sources_then_id() -> None:
    decisions = gate1(
        [GateInput(7, 5.0, 3), GateInput(3, 5.0, 3), GateInput(9, 5.0, 8)],
        GATE,
    )
    assert {i for i, d in decisions.items() if d.shortlisted} == {9, 3}


def test_quotes_prefer_first_hand_and_distinct_sources() -> None:
    def q(sid: int, doc: int, first_hand: bool = True, tier: str = "medium") -> dict:
        return {"signal_id": sid, "document_id": doc, "first_hand": first_hand, "tier": tier,
                "verified": "exact", "snippet_only": False, "published": None}  # fmt: skip

    quotes = [q(1, 1, first_hand=False), q(2, 2, tier="low"), q(3, 2), q(4, 3), q(5, 4)]
    units = {1: 1, 2: 2, 3: 3, 4: 3}  # documents 3 and 4: one source
    tiers = DEFAULTS.strength.tier_weights
    # One per source first (best quote of each), then the best of the rest.
    assert [p["signal_id"] for p in pick_quotes(quotes, units, tiers, 3)] == [3, 4, 1]
    assert [p["signal_id"] for p in pick_quotes(quotes, units, tiers, 5)] == [3, 4, 1, 5, 2]


# --- pipeline (DB) ---------------------------------------------------------------------------


def _seed(db, run_id: int) -> dict[str, int]:
    """Three documents with one signal each, two clusters, and stage metrics for the funnel."""
    now = datetime(2026, 9, 1, tzinfo=UTC)
    ids: dict[str, int] = {}
    with db.begin() as session:
        docs = [
            Document(run_id=run_id, url=f"https://f{i}.com/t", canonical_url=f"https://f{i}.com/t",
                     domain=f"f{i}.com", title=f"t{i}", source_category="forum",
                     quality_tier="medium", published_at=now)
            for i in range(3)
        ]  # fmt: skip
        session.add_all(docs)
        session.flush()
        signal_ids = []
        for i, doc in enumerate(docs):
            excerpt = Excerpt(
                run_id=run_id, document_id=doc.id, quote=f"İrsaliyeleri elle giriyoruz {i}.",
                translation=f"We enter delivery notes by hand {i}.", char_start=0, char_end=30,
                verified="exact", source="text",
            )  # fmt: skip
            session.add(excerpt)
            session.flush()
            signal = Signal(
                run_id=run_id, excerpt_id=excerpt.id, type="complaint", actor="nakliyeci",
                workflow="delivery notes", statement=f"Carrier {i} re-types notes.",
                first_hand=i != 1,
            )  # fmt: skip
            session.add(signal)
            session.flush()
            signal_ids.append(signal.id)
        session.add(IndependenceGroup(run_id=run_id, rule="same_author",
                                      document_ids=[docs[0].id, docs[1].id]))  # fmt: skip
        strong = ProblemCluster(
            run_id=run_id, name="Manual delivery-note entry", description="Carriers re-type notes.",
            signal_ids=signal_ids, independent_source_count=3, evidence_strength=5.5,
            source_category_mix={"forum": 3}, signal_type_mix={"complaint": 3},
            strength={"score": 5.5, "independent_sources": 3,
                      "components": {"sources": 0.58, "directness": 0.67}}, rank=1,
        )  # fmt: skip
        weak = ProblemCluster(
            run_id=run_id, name="Fuel card reconciliation", description="One forum post.",
            signal_ids=signal_ids[:1], independent_source_count=1, evidence_strength=2.1, rank=2,
        )  # fmt: skip
        session.add_all([strong, weak])
        for stage, metrics in [
            ("fetch", {"documents": 3}),
            ("extract", {"signals": 3, "quote_pass_rate": 0.97, "quotes_fuzzy": 1}),
            ("cluster", {"clusters": 2, "noise": 0}),
        ]:
            session.add(StageRun(run_id=run_id, stage=stage, status="completed", metrics=metrics))
        session.flush()
        ids.update(strong=strong.id, weak=weak.id, doc0=docs[0].id)
    return ids


def test_shortlist_stage_and_landscape_report(db, tmp_path) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    ids = _seed(db, run_id)
    ctx = make_context(db, run_id, FakeSearch())
    ctx.defaults = ctx.defaults.model_copy(update={"shortlist": GATE})

    row = run_stage(ctx, Shortlist())
    assert row.status == "completed"
    assert row.metrics == {
        "clusters": 2, "shortlisted": 1, "insufficient_evidence": 1,
        "failed_by_rule": {"min_strength": 1, "min_independent_sources": 1},
    }  # fmt: skip
    with db() as session:
        clusters = {c.id: c for c in session.scalars(select(ProblemCluster))}
    assert clusters[ids["strong"]].shortlisted and not clusters[ids["weak"]].shortlisted
    assert [t["rule"] for t in clusters[ids["weak"]].gate_trace] == [
        "min_strength", "min_independent_sources",
    ]  # fmt: skip

    report = build_landscape(db, run_id, ctx.defaults)
    (problem,) = report["shortlist"]
    assert problem["name"] == "Manual delivery-note entry"
    assert problem["first_hand_share"] == 0.67
    # Documents 0 and 1 share an author: the first quotes come from distinct sources.
    first_two = {q["document_id"] for q in problem["quotes"][:2]}
    assert len(first_two) == 2 and ids["doc0"] in first_two
    (weak,) = report["insufficient_evidence"]
    assert weak["failed_rules"] == [
        "evidence strength below the bar", "too few independent sources",
    ]  # fmt: skip
    funnel = {r["step"]: r["count"] for r in report["funnel"]}
    assert (funnel["Documents"], funnel["Verified signals"], funnel["Queries"]) == (3, 3, None)
    assert funnel["Shortlisted (Gate 1)"] == 1
    assert report["quality"]["quote_pass_rate"] == 0.97
    assert report["gate"]["min_strength"] == 4.0

    paths = render_landscape(report, tmp_path)
    assert [p.name for p in paths] == ["landscape.json", "landscape.md", "landscape.html"]
    md = (tmp_path / "landscape.md").read_text(encoding="utf-8")
    html = (tmp_path / "landscape.html").read_text(encoding="utf-8")
    assert "### 1. Manual delivery-note entry" in md
    assert "İrsaliyeleri elle giriyoruz" in md and "İrsaliyeleri elle giriyoruz" in html
    assert "Fuel card reconciliation" in md and "too few independent sources" in html
    assert "<script" not in html


def test_shortlist_needs_clusters(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    ctx = make_context(db, run_id, FakeSearch())
    try:
        run_stage(ctx, Shortlist())
    except ValueError as exc:
        assert "run cluster first" in str(exc)
    else:
        raise AssertionError("shortlist ran without clusters")


# --- verify (M4) -----------------------------------------------------------------------------

SUPPORTS = PARAGRAPH.split(". ")[0] + "."
COUNTER = PARAGRAPH.split(". ")[1].strip()
UNRELATED = OTHER.strip()
VERIFY_GATE = GATE.model_copy(update={"min_strength": 1.0, "max_shortlisted": 10})


def _verify_page(path: str) -> str:
    return PARAGRAPH + OTHER


def _verify_signal(quote: str, stance: str, statement: str) -> VerifySignal:
    return VerifySignal(
        quote=quote, translation="t", type="complaint", actor="nakliyeci", workflow="delivery",
        statement=statement, first_hand=True, submarket=None, author=None, stance=stance,
    )  # fmt: skip


def _verify_extract(prompt_input: str) -> VerifyExtractionBatch:
    assert prompt_input.startswith("# Problem under verification\n")
    return VerifyExtractionBatch(
        signals=[
            _verify_signal(SUPPORTS, "supports", "A carrier tracks shipments in Excel."),
            _verify_signal(COUNTER, "counter", "Operations re-key messages every evening."),
            _verify_signal(UNRELATED, "unrelated", "Customs brokers chase documents."),
        ]
    )


def _entail(rejected: set[str]):
    """Entailment answer: claims whose statement is in ``rejected`` are not supported."""

    def answer(prompt_input: str) -> EntailmentBatch:
        items = json.loads(prompt_input.split("# Claims\n", 1)[1])
        return EntailmentBatch(
            verdicts=[
                EntailmentVerdict(
                    item=i["item"],
                    verdict="not_supported" if i["claim"] in rejected else "supported",
                    note="n",
                )
                for i in items
            ]
        )

    return answer


def _verify_script(prompt_input: str) -> LoopAction:
    """Search once, read the first hit, finish."""
    history = prompt_input.split("# History\n", 1)[1]
    done = len(re.findall(r"^step \d+:", history, re.MULTILINE))
    script = [
        LoopAction(action="search", query="irsaliye elle giriş forum", reason="r"),
        LoopAction(action="fetch", hit_id=1, reason="r"),
    ]
    return script[done] if done < len(script) else LoopAction(action="finish", reason="done")


def _seed_verify(db, run_id: int, clusters: int = 1) -> list[int]:
    """Per cluster: three collected forum documents, one extract signal + fact each, the
    cluster's inference claim, and a passing Gate-1 decision."""
    now = datetime.now(UTC)
    cluster_ids = []
    with db.begin() as session:
        for k in range(clusters):
            fact_ids, signal_ids = [], []
            for i in range(3):
                doc = Document(
                    run_id=run_id, url=f"https://f{k}{i}.com/t",
                    canonical_url=f"https://f{k}{i}.com/t", domain=f"f{k}{i}.com",
                    source_category="forum", quality_tier="medium", published_at=now,
                )  # fmt: skip
                session.add(doc)
                session.flush()
                excerpt = Excerpt(
                    run_id=run_id, document_id=doc.id, quote=f"İrsaliye {k}{i}.", translation="t",
                    verified="exact", source="text",
                )  # fmt: skip
                session.add(excerpt)
                session.flush()
                signal = Signal(
                    run_id=run_id, excerpt_id=excerpt.id, type="complaint", actor="nakliyeci",
                    workflow="delivery notes", statement=f"Carrier {k}{i} re-types notes.",
                    first_hand=True,
                )  # fmt: skip
                session.add(signal)
                session.flush()
                fact = add_fact(session, run_id, signal.statement, [excerpt.id], stage="extract")
                fact_ids.append(fact.id)
                signal_ids.append(signal.id)
            inference = add_inference(
                session, run_id, "Carriers re-type notes.", fact_ids, stage="cluster"
            )
            cluster = ProblemCluster(
                run_id=run_id, name=f"Manual delivery-note entry {k}",
                description="Carriers re-type delivery notes.", signal_ids=signal_ids,
                independent_source_count=3, evidence_strength=5.0,
                strength={"score": 5.0, "independent_sources": 3, "components": {}},
                shortlisted=True, claim_id=inference.id,
                gate_trace=[{"rule": "min_strength", "value": 5.0, "threshold": 1.0,
                             "passed": True}],
            )  # fmt: skip
            session.add(cluster)
            session.flush()
            cluster_ids.append(cluster.id)
    return cluster_ids


def _verify_context(db, run_id: int, rejected: frozenset[str] = frozenset()):
    llm = FakeLLM(
        {
            LoopAction: _verify_script,
            VerifyExtractionBatch: _verify_extract,
            EntailmentBatch: _entail(set(rejected)),
        }
    )
    ctx = make_context(db, run_id, FakeSearch(), llm, page_text=_verify_page)
    ctx.defaults = ctx.defaults.model_copy(update={"shortlist": VERIFY_GATE})
    return ctx, llm


def _counts(db) -> dict[str, int]:
    with db() as session:
        return {
            model.__name__: session.scalar(select(func.count()).select_from(model))
            for model in (Document, Excerpt, Signal, Claim, Query, SearchResult, IndependenceGroup)
        }


def test_verify_adds_evidence_flags_counter_signals_and_rechecks_gate1(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    (cluster_id,) = _seed_verify(db, run_id)
    ctx, llm = _verify_context(db, run_id, rejected=frozenset({"Carrier 01 re-types notes."}))

    row = run_stage(ctx, Verify())
    assert row.status == "completed", row.error
    m = row.metrics
    assert (m["problems"], m["passed"], m["new_signals"], m["counter_signals"]) == (1, 1, 1, 1)
    assert m["unrelated_dropped"] == 1
    assert m["entailment"]["supported"] == 3 and m["entailment"]["not_supported"] == 1

    with db() as session:
        cluster = session.get(ProblemCluster, cluster_id)
        v = cluster.verification
        (doc,) = session.scalars(select(Document).where(Document.origin == "verify")).all()
        (query,) = session.scalars(select(Query).where(Query.intent == "verify")).all()
        added = session.scalars(select(Signal).where(Signal.id.in_(v["signal_ids_added"]))).all()
        counter = session.scalars(
            select(Signal).where(Signal.id.in_(v["counter_signal_ids"]))
        ).all()
        rejected = session.scalar(
            select(Claim).where(Claim.statement == "Carrier 01 re-types notes.")
        )
        verify_excerpts = session.scalars(select(Excerpt).where(Excerpt.stage == "verify")).all()
    assert cluster.shortlisted and v["passed"]
    assert (doc.problem_id, doc.canonical_url) == (cluster_id, "https://forum.com/shared")
    assert query.meta["origin"] == "loop" and query.meta["problem_ids"] == [cluster_id]
    assert [s.meta for s in added] == [
        {"origin": "verify", "problem_id": cluster_id, "counter": False}
    ]
    assert [s.meta["counter"] for s in counter] == [True]
    assert {e.document_id for e in verify_excerpts} == {doc.id}
    assert {e.quote for e in verify_excerpts} == {SUPPORTS, COUNTER}
    # The not-supported fact stops counting; the counter signal never counts.
    assert rejected.entailment == "not_supported" and rejected.entailment_checked
    assert len(v["excluded_signal_ids"]) == 1
    assert v["strength_before"]["independent_sources"] == 3
    assert v["strength_after"]["independent_sources"] == 3  # 2 collected + 1 verify document
    assert [t["rule"] for t in cluster.gate_trace] == [
        "min_strength", "verify_min_strength", "verify_min_independent_sources",
        "verify_min_key_claims_supported",
    ]  # fmt: skip
    # Gate 1's own columns are untouched; the helpers combine both.
    assert cluster.evidence_strength == 5.0 and len(cluster.signal_ids) == 3
    assert cluster_signal_ids(cluster) == sorted(cluster.signal_ids + v["signal_ids_added"])
    assert current_strength(cluster) == v["strength_after"]["score"]
    # A re-run of cluster reads only extract's signals, never verify's.
    loaded, _, _ = load_evidence(ctx)
    assert {s.id for s in loaded} == set(cluster.signal_ids)

    # Re-running replaces verify's outputs: same rows, answers from the LLM cache.
    counts, calls = _counts(db), len(llm.calls)
    assert run_stage(ctx, Verify()).status == "completed"
    assert _counts(db) == counts and len(llm.calls) == calls
    with db() as session:
        again = session.get(ProblemCluster, cluster_id)
    assert len(again.gate_trace) == 4 and again.verification["passed"]


def test_landscape_report_shows_the_second_round(db, tmp_path) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    _seed_verify(db, run_id)
    ctx, _ = _verify_context(db, run_id)
    run_stage(ctx, Verify())

    report = build_landscape(db, run_id, ctx.defaults)
    (problem,) = report["shortlist"]
    v = problem["verification"]
    assert (v["new_signals"], v["counter_signals"], v["key_claims_supported"]) == (1, 1, 3)
    assert [q["quote"] for q in v["counter_quotes"]] == [COUNTER]
    assert problem["signals"] == 3 and any(q["quote"] == SUPPORTS for q in problem["quotes"])
    funnel = {r["step"]: r["count"] for r in report["funnel"]}
    assert funnel["Still shortlisted after verify"] == 1

    render_landscape(report, tmp_path)
    md = (tmp_path / "landscape.md").read_text(encoding="utf-8")
    html = (tmp_path / "landscape.html").read_text(encoding="utf-8")
    assert "After verification: strength 5.0 →" in md and "**Counter-evidence**" in md
    assert COUNTER in md and "Counter-evidence</h4>" in html


def test_verify_failure_unshortlists_and_shortlist_restores(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    (cluster_id,) = _seed_verify(db, run_id)
    rejected = frozenset(f"Carrier 0{i} re-types notes." for i in range(3))
    ctx, _ = _verify_context(db, run_id, rejected=rejected)

    row = run_stage(ctx, Verify())
    assert (row.metrics["passed"], row.metrics["failed_verify"]) == (0, 1)
    with db() as session:
        cluster = session.get(ProblemCluster, cluster_id)
    assert not cluster.shortlisted and not cluster.verification["passed"]
    failed = [t["rule"] for t in cluster.gate_trace if not t["passed"]]
    assert failed == ["verify_min_independent_sources", "verify_min_key_claims_supported"]

    # Re-running Gate 1 restores its own decision and drops the stale second round.
    run_stage(ctx, Shortlist())
    with db() as session:
        cluster = session.get(ProblemCluster, cluster_id)
    assert cluster.verification is None
    assert not any(t["rule"].startswith("verify_") for t in cluster.gate_trace)


def test_problems_reading_the_same_page_share_one_document(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    a, b = _seed_verify(db, run_id, clusters=2)
    ctx, _ = _verify_context(db, run_id)

    row = run_stage(ctx, Verify())
    assert row.status == "completed" and row.metrics["passed"] == 2
    with db() as session:
        docs = session.scalars(select(Document).where(Document.origin == "verify")).all()
        (query,) = session.scalars(select(Query).where(Query.intent == "verify")).all()
        clusters = {c.id: c for c in session.scalars(select(ProblemCluster))}
    assert len(docs) == 1 and docs[0].problem_id == a
    assert sorted(query.meta["problem_ids"]) == [a, b]
    # Each problem keeps its own signals from the shared page.
    added_a = clusters[a].verification["signal_ids_added"]
    added_b = clusters[b].verification["signal_ids_added"]
    assert len(added_a) == len(added_b) == 1 and added_a != added_b


def test_same_author_across_collected_and_verify_documents_is_one_source(db) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    _seed_verify(db, run_id)
    ctx, _ = _verify_context(db, run_id)
    with db.begin() as session:
        collected = session.scalars(select(Document).order_by(Document.id)).first()
        verify_doc = Document(
            run_id=run_id, url="https://v.com/x", canonical_url="https://v.com/x",
            domain="v.com", origin="verify", quality_tier="low",
        )  # fmt: skip
        session.add(verify_doc)
        session.flush()
        session.execute(
            update(Excerpt).where(Excerpt.document_id == collected.id).values(author_hash="h")
        )
        session.add(
            Excerpt(run_id=run_id, document_id=verify_doc.id, quote="q", verified="exact",
                    author_hash="h", stage="verify")
        )  # fmt: skip
        session.flush()
        written = write_independence(session, ctx, {verify_doc.id: "kısa metin"}, set())
        groups = session.scalars(select(IndependenceGroup.document_ids)).all()
    assert written == {"same_author": 1}
    assert groups == [sorted([collected.id, verify_doc.id])]


def test_regate_needs_supported_key_claims() -> None:
    cfg = DEFAULTS.verify
    trace = regate(6.0, 4, 1, 5, VERIFY_GATE, cfg)
    assert [t["passed"] for t in trace] == [True, True, False]
    assert trace[2]["threshold"] == cfg.min_key_claims_supported
    # Fewer key claims than the minimum: all of them must hold.
    assert regate(6.0, 4, 1, 1, VERIFY_GATE, cfg)[2]["passed"]
    assert gate1_trace([{"rule": "min_strength"}, {"rule": "verify_min_strength"}]) == [
        {"rule": "min_strength"}
    ]
