"""M7 multi-market evaluation: cost per run from the ledger and the stages' search counts."""

from datetime import timedelta
from decimal import Decimal

from fakes import PACK, PLAN

from signalforge.config import get_defaults
from signalforge.db.models import LLMCall, StageRun
from signalforge.pipeline.runner import create_run
from signalforge.reporting.evaluation import evaluate_runs, render_evaluation

DEFAULTS = get_defaults()


def _stage(session, run_id: int, stage: str, cost: str, metrics: dict, minutes: int = 1) -> None:
    row = StageRun(run_id=run_id, stage=stage, status="completed", cost_usd=Decimal(cost),
                   metrics=metrics)  # fmt: skip
    session.add(row)
    session.flush()
    row.finished_at = row.started_at + timedelta(minutes=minutes)
    session.add(LLMCall(run_id=run_id, stage=stage, tier="fast", model="m", prompt_id="p",
                        prompt_version=1, input_hash="h", cost_usd=Decimal(cost)))  # fmt: skip


def test_cost_per_run_counts_one_clean_pass(db, tmp_path) -> None:
    run_id = create_run(db, PLAN, PACK, DEFAULTS)
    with db.begin() as session:
        _stage(session, run_id, "search", "0", {"searched_live": 140})
        _stage(session, run_id, "extract", "0.05", {"signals": 3})  # re-run below
        _stage(session, run_id, "extract", "0.04", {"signals": 4, "quote_pass_rate": 0.99})
        _stage(session, run_id, "verify", "0.02", {"searches_live": 10, "passed": 2})
        _stage(session, run_id, "competitors", "0.03", {"searches_live": 25})

    [row] = evaluate_runs(db, [run_id], DEFAULTS)
    assert row.market == PLAN.request.industry
    assert row.live_searches == 175
    assert row.search_usd == round(175 * DEFAULTS.search.usd_per_query["serper"], 4)
    assert round(row.llm_clean_usd, 4) == 0.09  # the latest extract only
    assert round(row.llm_spent_usd, 4) == 0.14  # every call
    assert round(row.llm_by_agent["problem_discovery"], 4) == 0.04
    assert row.clean_usd == round(0.09 + row.search_usd, 4)
    assert row.metric("extract", "signals") == 4
    assert row.wall_s == 4 * 60

    text = render_evaluation([row], tmp_path / "evaluation.md").read_text(encoding="utf-8")
    assert f"| {row.market} | {run_id} | $0.090 | 175 |" in text
    assert "not labelled" in text
