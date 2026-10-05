"""M3 evidence validator part 1: Gate 1 (shortlist stage) and the problem landscape report.

The DB tests seed documents, excerpts, signals and clusters directly, so they do not depend on the
extract / cluster stages (problem_discovery).
"""

from datetime import UTC, datetime

from fakes import PACK, PLAN, FakeSearch, make_context
from sqlalchemy import select

from signalforge.config import get_defaults
from signalforge.db.models import (
    Document,
    Excerpt,
    IndependenceGroup,
    ProblemCluster,
    Signal,
    StageRun,
)
from signalforge.pipeline.runner import create_run, run_stage
from signalforge.pipeline.stages.shortlist import GateInput, Shortlist, gate1
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
