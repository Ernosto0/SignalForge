"""Benchmark metrics and replay over a frozen fixture (reporting/benchmark.py)."""

import json

from fakes import PACK, PLAN, FakeSearch, make_context
from sqlalchemy import text

from signalforge.config import CacheMode, get_defaults
from signalforge.db.base import Base
from signalforge.db.models import Query
from signalforge.fixtures import export_runs, import_fixture
from signalforge.pipeline.runner import create_run, run_pipeline
from signalforge.pipeline.stages import STAGES
from signalforge.reporting.benchmark import (
    Metric,
    compare,
    load_benchmark,
    pool,
    render_benchmark,
    replay_scenario,
    run_benchmark,
)

COLLECTION = STAGES[1:5]
QUERIES = ["irsaliye excel", "bad gümrük evrak", "depo sayım farkı"]


def _queries(db, run_id: int) -> None:
    with db.begin() as session:
        session.add_all(
            Query(run_id=run_id, text=t, lang="tr", intent="pain", meta={}) for t in QUERIES
        )


def _manifest(tmp_path, run_id: int) -> object:
    (tmp_path / "proxy.tsv").write_text("market\trun\topportunity\tverdict\n", encoding="utf-8")
    (tmp_path / "bench.yaml").write_text(
        "version: 1\n"
        "proxy_labels: [{file: proxy.tsv, labeler: 'proxy:test', target_type: opportunity}]\n"
        f"scenarios: [{{id: tr-logistics, industry: logistics, fixture: fx, run: {run_id}, "
        "split: dev}]\n",
        encoding="utf-8",
    )
    return load_benchmark(tmp_path / "bench.yaml")


def test_pooling_sums_shares_and_skips_unavailable() -> None:
    a, b = Metric("q", "deterministic", 9, 10), Metric("q", "deterministic", 1, 10)
    assert pool([a, b]).value == 0.5
    assert pool([a, Metric("q", "deterministic", unavailable="no data")]).value == 0.9
    assert pool([Metric("q", "deterministic", unavailable="no data")]) is None
    costs = [Metric("c", "deterministic", v, None, "median") for v in (1.0, 3.0, 2.0)]
    assert pool(costs).value == 2.0
    # An empty share has no value instead of a made-up one.
    assert Metric("q", "deterministic", 0, 0).value is None


def test_benchmark_reports_tiers_unavailable_metrics_and_a_faithful_replay(
    db, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("signalforge.providers.search.base.time.sleep", lambda _: None)
    run_id = create_run(db, PLAN, PACK, get_defaults())
    _queries(db, run_id)
    run_pipeline(make_context(db, run_id, FakeSearch()), COLLECTION)
    export_runs(db, [run_id], tmp_path / "fx")
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with db.begin() as session:
        session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    import_fixture(db, tmp_path / "fx")
    bench = _manifest(tmp_path, run_id)

    def no_page(path: str) -> str:
        raise AssertionError(f"replay fetched {path}")

    def replay_context(new_id: int, pack_id: str):
        _queries(db, new_id)  # query_gen needs the real model; the recording started at search
        ctx = make_context(db, new_id, FakeSearch(), page_text=no_page)
        ctx.cache.mode = CacheMode.REPLAY
        return ctx

    def replay(scenario):
        return replay_scenario(db, scenario, COLLECTION, replay_context, get_defaults())

    result = run_benchmark(db, bench, get_defaults(), replay)

    (scenario,) = result["scenarios"]
    m = scenario["metrics"]
    assert m["duplicate_collapse_rate"] == {"tier": "deterministic", "value": 0.75, "n": 4}
    assert m["documents"]["value"] == 4
    assert m["fact_checked_share"]["tier"] == "llm_judged"
    # No labels, no cards: label metrics are unavailable with a reason, never zero.
    assert "unavailable" in m["precision_at_3_proxy"]
    assert m["problem_discovery_recall"]["unavailable"].startswith("no reference problem list")
    assert "unavailable" in m["attractiveness_judge_range_median"]
    assert result["pooled"]["dev"]["documents"]["value"] == 4

    r = scenario["replay"]
    assert r["stages"] == [s.name for s in COLLECTION]
    assert r["mismatched"] == {} and r["stopped_at"] is None and r["error"] is None

    md, js = render_benchmark(result, tmp_path / "out")
    assert "## Replay" in md.read_text(encoding="utf-8")
    assert compare(result, json.loads(js.read_text(encoding="utf-8"))) == []
    changed = json.loads(js.read_text(encoding="utf-8"))
    changed["pooled"]["dev"]["documents"]["value"] = 3
    assert compare(result, changed) == [("dev", "documents", 3, 4)]
