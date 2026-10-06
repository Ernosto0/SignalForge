"""M2 collection: search → triage → fetch → dedupe."""

from datetime import UTC, datetime

import pytest
from fakes import OTHER, PACK, PARAGRAPH, PLAN, FakeJudge, FakeSearch, make_context
from sqlalchemy import select

from signalforge.config import get_defaults
from signalforge.db.models import (
    Document,
    IndependenceGroup,
    Query,
    ResearchRun,
    SearchResult,
    StageRun,
    UrlCandidate,
)
from signalforge.evidence.dedup import DocText, find_duplicates
from signalforge.pipeline.runner import create_run, latest_stage_runs, run_pipeline
from signalforge.pipeline.stages import STAGES
from signalforge.pipeline.stages.fetch import collect
from signalforge.pipeline.stages.triage import ResultRow, build_candidates, triage
from signalforge.providers.fetch import FetchedPage, FetchStatus
from signalforge.text import guess_language

DEDUPE = get_defaults().dedupe
TRIAGE = get_defaults().triage


# --- dedup ----------------------------------------------------------------------------------


def test_exact_and_near_duplicates_are_grouped_by_rule() -> None:
    near = PARAGRAPH * 3 + "Kaynak: sektör dergisi."
    docs = [
        DocText(1, "a.com", PARAGRAPH * 3),
        DocText(2, "a.com", (PARAGRAPH * 3).upper()),  # same text after casefold
        DocText(3, "b.com", near),  # republished elsewhere with a credit line
        DocText(4, "c.com", OTHER * 3),
    ]
    groups = find_duplicates(docs, DEDUPE)

    assert [(g.rule, g.document_ids) for g in groups] == [("syndicated", [1, 2, 3])]


def test_same_domain_near_duplicates_are_near_dup() -> None:
    docs = [DocText(1, "a.com", PARAGRAPH * 3), DocText(2, "a.com", PARAGRAPH * 3 + "Sayfa 2")]
    assert [(g.rule, g.document_ids) for g in find_duplicates(docs, DEDUPE)] == [
        ("near_dup", [1, 2])
    ]


def test_short_texts_only_match_exactly() -> None:
    docs = [DocText(1, "a.com", "Excel ile takip"), DocText(2, "b.com", "Excel ile takip ediyoruz")]
    assert find_duplicates(docs, DEDUPE) == []


def test_guess_language() -> None:
    assert guess_language(PARAGRAPH * 2) == "tr"
    assert guess_language("We track all of the shipments in a spreadsheet and it is slow " * 3) == (
        "en"
    )
    assert guess_language("kısa") is None


# --- triage ---------------------------------------------------------------------------------


def _row(url: str, qid: int, rank: int, title: str = "t", hint: str | None = None) -> ResultRow:
    return ResultRow(url, title, f"snippet {title}", rank, qid, f"query {qid}", hint)


def test_candidates_collapse_urls_and_flag_off_site_hits() -> None:
    rows = [
        _row("https://www.sikayetvar.com/x?utm_source=g", 1, 4),
        _row("https://sikayetvar.com/x", 2, 1, title="best"),
        _row("https://random.com/ilan", 3, 2, hint="kariyer.net"),
        _row("https://www.kariyer.net/is-ilani/1", 3, 1, hint="kariyer.net"),
    ]
    by_url = {c.canonical_url: c for c in build_candidates(rows, PACK)}

    complaint = by_url["https://sikayetvar.com/x"]
    assert (complaint.query_ids, complaint.best_rank, complaint.title) == ([1, 2], 1, "best")
    assert (complaint.source_category, complaint.quality_tier) == ("complaints", "medium")
    assert by_url["https://random.com/ilan"].off_site
    assert not by_url["https://kariyer.net/is-ilani/1"].off_site
    assert by_url["https://random.com/ilan"].quality_tier == PACK.default_tier


def test_triage_applies_tier_bars_domain_cap_and_reserve() -> None:
    cfg = TRIAGE.model_copy(update={"max_urls": 3, "max_per_domain": 2, "batch_size": 3})
    rows = [
        _row("https://gib.gov.tr/a", 1, 1, title="s1"),  # high tier: 1 passes
        _row("https://blog.com/a", 1, 2, title="s1"),  # low tier: 1 fails
        _row("https://kariyer.net/1", 2, 1, title="s3"),
        _row("https://kariyer.net/2", 2, 2, title="s3"),
        _row("https://kariyer.net/3", 2, 3, title="s3"),  # third on one domain → reserve
        _row("https://blog.com/b", 3, 1, title="s2"),
        _row("https://youtube.com/watch?v=1", 3, 2, title="s3"),
        _row("https://gib.gov.tr/k.pdf", 3, 3, title="s3"),
    ]
    judge = FakeJudge(skip={"s2"})
    result = triage(rows, PLAN, PACK, cfg, judge)
    by_url = {c.canonical_url: c for c in result.candidates}

    decisions = {u.split("//")[1]: (c.decision, c.decision_rule) for u, c in by_url.items()}
    assert decisions == {
        "kariyer.net/1": ("keep", "llm"),
        "kariyer.net/2": ("keep", "llm"),
        "blog.com/b": ("keep", "llm"),  # skipped once, judged in the second round
        "kariyer.net/3": ("reserve", "llm"),
        "gib.gov.tr/a": ("reserve", "llm"),  # passes, but max_urls is full
        "blog.com/a": ("drop", "below_min_score"),
        "youtube.com/watch?v=1": ("drop", "skip_domain"),
        "gib.gov.tr/k.pdf": ("drop", "file_type"),
    }
    ranked = [c for c in result.candidates if c.priority is not None]
    priorities = sorted((c.priority, c.canonical_url) for c in ranked)
    assert [u.split("//")[1] for _, u in priorities] == [
        "kariyer.net/1", "kariyer.net/2", "blog.com/b", "kariyer.net/3", "gib.gov.tr/a",
    ]  # fmt: skip
    m = result.metrics
    assert (m["rejudged"], m["unjudged"], m["domain_capped"]) == (1, 0, 1)
    assert m["unknown_ids"] == judge.calls
    assert (m["prefilter_skip_domain"], m["prefilter_file_type"]) == (1, 1)


# --- fetch ----------------------------------------------------------------------------------


def _candidate(url: str, priority: int, decision: str = "keep", access: str = "fetch"):
    return UrlCandidate(
        id=priority, url=url, canonical_url=url, domain=url.split("/")[2], title="t",
        snippet="WhatsApp ile takip ediyoruz", quality_tier="medium", source_category=None,
        access=access, decision=decision, priority=priority,
    )  # fmt: skip


def _page(url: str, status: FetchStatus = FetchStatus.OK, final: str | None = None) -> FetchedPage:
    return FetchedPage(
        url=url,
        canonical_url=url,
        status=status,
        fetched_at=datetime.now(UTC),
        final_url=final or url,
        text=PARAGRAPH * 2 if status is FetchStatus.OK else None,
        published_at="2025-02-03",
    )


def test_fetch_backfills_failures_from_reserve_and_collapses_redirects() -> None:
    pages = {
        "https://a.com/1": _page("https://a.com/1"),
        "https://a.com/2": _page("https://a.com/2", FetchStatus.HTTP_ERROR),
        "https://a.com/3": _page("https://a.com/3", final="https://a.com/1"),  # redirect dup
        "https://b.com/r1": _page("https://b.com/r1"),
        "https://b.com/r2": _page("https://b.com/r2"),
        "https://b.com/r3": _page("https://b.com/r3"),
    }
    candidates = [
        _candidate("https://a.com/1", 0),
        _candidate("https://a.com/2", 1),
        _candidate("https://a.com/3", 2),
        _candidate("https://linkedin.com/p", 3, access="snippet_only"),
        _candidate("https://b.com/r1", 4, "reserve"),
        _candidate("https://b.com/r2", 5, "reserve"),
        _candidate("https://b.com/r3", 6, "reserve"),
    ]
    cfg = get_defaults().fetch_stage.model_copy(update={"max_documents": 4, "max_attempts": 10})
    fetched = collect(candidates, lambda url: pages[url], PACK, cfg)

    docs = [o.document for o in fetched.outcomes if o.document]
    assert [d.canonical_url for d in docs] == [
        "https://a.com/1", "https://linkedin.com/p", "https://b.com/r1", "https://b.com/r2",
    ]  # fmt: skip
    assert docs[1].snippet_only and not docs[0].snippet_only
    assert docs[0].published_at == datetime(2025, 2, 3, tzinfo=UTC)
    assert docs[0].lang == "tr"
    m = fetched.metrics
    assert (m["attempted"], m["fetched_ok"], m["documents"], m["from_reserve"]) == (5, 4, 4, 2)
    assert m["by_status"] == {"ok": 3, "http_error": 1, "duplicate_url": 1, "snippet_only": 1}
    assert m["fetch_success_rate"] == 0.8


def test_fetch_stops_at_max_attempts() -> None:
    candidates = [_candidate(f"https://a.com/{i}", i) for i in range(5)]
    cfg = get_defaults().fetch_stage.model_copy(update={"max_documents": 5, "max_attempts": 3})
    fetched = collect(candidates, lambda url: _page(url, FetchStatus.NO_TEXT), PACK, cfg)
    assert fetched.metrics["attempted"] == 3
    assert fetched.metrics["documents"] == 0


# --- pipeline (DB) ---------------------------------------------------------------------------


def test_collection_pipeline_end_to_end(db, monkeypatch) -> None:
    monkeypatch.setattr("signalforge.providers.search.base.time.sleep", lambda _: None)
    run_id = create_run(db, PLAN, PACK, get_defaults())
    with db.begin() as session:
        session.add_all(
            Query(run_id=run_id, text=t, lang="tr", intent="pain", meta={})
            for t in ["irsaliye excel", "bad gümrük evrak", "depo sayım farkı"]
        )
    search = FakeSearch()
    ctx = make_context(db, run_id, search)

    stages = STAGES[1:5]  # query_gen needs the real model; queries were inserted above
    run_pipeline(ctx, stages)

    latest = latest_stage_runs(db, run_id)
    assert [latest[s.name].status for s in stages] == ["completed"] * 4
    m = {name: row.metrics for name, row in latest.items()}
    assert (m["search"]["results"], m["search"]["unique_urls"], m["search"]["retried"]) == (9, 7, 1)
    assert (m["triage"]["keep"], m["triage"]["drop"]) == (4, 3)
    assert m["fetch"]["documents"] == 4
    assert m["fetch"]["fetch_success_rate"] == 1.0
    assert m["dedupe"]["groups"] == 1
    assert m["dedupe"]["duplicate_collapse_rate"] == 0.75
    assert m["dedupe"]["independent_sources"] == 1

    with db() as session:
        assert session.scalar(select(ResearchRun.status).where(ResearchRun.id == run_id)) == (
            "completed"
        )
        assert len(session.scalars(select(SearchResult)).all()) == 9
        assert len(session.scalars(select(Document)).all()) == 4
        kept = session.scalars(select(UrlCandidate).where(UrlCandidate.decision == "keep")).all()
        assert all(c.document_id and c.fetch_status == "ok" for c in kept)
        (group,) = session.scalars(select(IndependenceGroup)).all()
        assert (group.rule, len(group.document_ids)) == ("near_dup", 4)

    # Re-running from triage reuses cached searches, pages and judgments, and replaces outputs.
    calls = search.calls
    run_pipeline(ctx, stages, from_stage="triage")
    assert search.calls == calls
    with db() as session:
        assert len(session.scalars(select(Document)).all()) == 4
        assert len(session.scalars(select(IndependenceGroup)).all()) == 1
        triage_runs = session.scalars(select(StageRun).where(StageRun.stage == "triage")).all()
        assert len(triage_runs) == 2

    # Nothing left to resume; an unknown stage is rejected.
    assert run_pipeline(ctx, stages, resume=True) == []
    with pytest.raises(ValueError, match="unknown stage"):
        run_pipeline(ctx, stages, from_stage="cluster")


def test_from_stage_requires_earlier_stages(db) -> None:
    run_id = create_run(db, PLAN, PACK, get_defaults())
    ctx = make_context(db, run_id, FakeSearch())
    with pytest.raises(ValueError, match="earlier stages have not completed: search"):
        run_pipeline(ctx, STAGES[1:5], from_stage="triage")
