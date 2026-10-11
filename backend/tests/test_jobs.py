"""The Postgres job queue (pipeline/jobs.py): enqueue validation, SKIP LOCKED claims, stale
reclaim, cancel, and running jobs with fake stages."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from fakes import PACK, PLAN, FakeSearch, make_context
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from signalforge.agents import get_agent
from signalforge.config import get_defaults
from signalforge.db.models import Job, ResearchRun, StageRun
from signalforge.pipeline import jobs
from signalforge.pipeline.jobs import (
    JobArgs,
    JobConflict,
    cancel,
    claim_next,
    enqueue,
    heartbeat,
    pipeline_options,
    run_job,
    work,
)
from signalforge.pipeline.runner import StageResult, create_run

DEFAULTS = get_defaults()
STALE_S = 300


class FakeStage:
    def __init__(self, name: str, fail: type[BaseException] | None = None) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0

    def run(self, ctx) -> StageResult:
        self.calls += 1
        if self.fail is not None:
            raise self.fail("boom")
        return StageResult(metrics={"ok": True})


def fake_context(db, pack_id, run_id):
    return make_context(db, run_id, FakeSearch())


def _run(db) -> int:
    return create_run(db, PLAN, PACK, DEFAULTS)


def _job(db, job_id: int) -> Job:
    with db() as session:
        return session.get_one(Job, job_id)


def _stage_runs(db, run_id: int) -> list[StageRun]:
    with db() as session:
        return list(
            session.scalars(select(StageRun).where(StageRun.run_id == run_id).order_by(StageRun.id))
        )


def _run_status(db, run_id: int) -> str:
    with db() as session:
        return session.get_one(ResearchRun, run_id).status


# --- enqueue --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"agent": "report_writer", "resume": True},
        {"agent": "report_writer", "from_stage": "score"},
        {"agent": "nope"},
        {"from_stage": "nope"},
        {"until": "query_gen", "from_stage": "report"},
        {"stage": "report"},  # unknown key
    ],
)
def test_enqueue_rejects_invalid_args(db, args) -> None:
    run_id = _run(db)
    with pytest.raises(ValueError):
        enqueue(db, run_id, args)
    with db() as session:
        assert session.scalars(select(Job)).all() == []


def test_agent_runs_its_first_to_last_stage() -> None:
    agent = get_agent("problem_discovery")
    assert pipeline_options(JobArgs(agent=agent.name)) == {
        "resume": False,
        "from_stage": agent.first,
        "until": agent.last,
    }


def test_enqueue_unknown_run(db) -> None:
    with pytest.raises(LookupError):
        enqueue(db, 999, {})


def test_one_active_job_per_run(db) -> None:
    run_id, other = _run(db), _run(db)
    first = enqueue(db, run_id, {"resume": True})

    with pytest.raises(JobConflict):
        enqueue(db, run_id, {})
    enqueue(db, other, {})  # other runs are independent
    cancel(db, first)
    enqueue(db, run_id, {})  # a finished job no longer blocks

    assert _job(db, first).args == {
        "resume": True,
        "from_stage": None,
        "until": None,
        "agent": None,
    }


def test_concurrent_enqueue_loses_on_the_unique_index(db, monkeypatch) -> None:
    run_id = _run(db)
    enqueue(db, run_id, {})
    # Both requests passed the "no active job" check: the partial unique index decides.
    monkeypatch.setattr(jobs, "ACTIVE", ())

    with pytest.raises(JobConflict):
        enqueue(db, run_id, {})
    with pytest.raises(IntegrityError), db.begin() as session:
        session.add(Job(run_id=run_id, status="running", args={}))


# --- claim, cancel, stale reclaim -----------------------------------------------------------


def test_claim_takes_the_oldest_queued_job_and_skips_locked_ones(db) -> None:
    first, second = enqueue(db, _run(db), {}), enqueue(db, _run(db), {})

    with db.begin() as locker:
        locker.scalar(select(Job).where(Job.id == first).with_for_update())
        claimed = claim_next(db, "w2", STALE_S)
    assert claimed is not None and claimed.id == second

    claimed = claim_next(db, "w1", STALE_S)
    assert claimed is not None and claimed.id == first
    assert claimed.status == "running" and claimed.worker_id == "w1"
    assert claimed.started_at is not None and claimed.heartbeat_at is not None
    assert claim_next(db, "w1", STALE_S) is None


def test_cancel_only_queued_jobs(db) -> None:
    job_id = enqueue(db, _run(db), {})
    cancelled = cancel(db, job_id)
    assert cancelled.status == "cancelled" and cancelled.finished_at is not None
    assert claim_next(db, "w", STALE_S) is None

    with pytest.raises(JobConflict):
        cancel(db, job_id)
    running = enqueue(db, _run(db), {})
    claim_next(db, "w", STALE_S)
    with pytest.raises(JobConflict):
        cancel(db, running)
    with pytest.raises(LookupError):
        cancel(db, 999)


def test_a_stale_running_job_is_failed_and_its_run_left_resumable(db) -> None:
    run_id = _run(db)
    job_id = enqueue(db, run_id, {})
    claim_next(db, "dead-worker", STALE_S)
    with db.begin() as session:
        session.add(StageRun(run_id=run_id, stage="query_gen", status="completed"))
        session.add(StageRun(run_id=run_id, stage="search", status="running"))
        session.get_one(ResearchRun, run_id).status = "running"
        session.get_one(Job, job_id).heartbeat_at = datetime.now(UTC) - timedelta(seconds=60)

    assert claim_next(db, "w", STALE_S) is None  # heartbeat is recent enough
    with db.begin() as session:
        session.get_one(Job, job_id).heartbeat_at = datetime.now(UTC) - timedelta(seconds=600)
    assert claim_next(db, "w", STALE_S) is None

    job = _job(db, job_id)
    assert (job.status, job.error) == ("failed", "worker lost")
    assert [(r.stage, r.status, r.error) for r in _stage_runs(db, run_id)] == [
        ("query_gen", "completed", None),
        ("search", "failed", "worker lost"),
    ]
    assert _run_status(db, run_id) == "failed"
    enqueue(db, run_id, {"resume": True})


def test_heartbeat_updates_the_job_until_stopped(db) -> None:
    job_id = enqueue(db, _run(db), {})
    claim_next(db, "w", STALE_S)
    before = _job(db, job_id).heartbeat_at

    with heartbeat(db, job_id, 0.01):
        time.sleep(0.2)
    after = _job(db, job_id).heartbeat_at
    time.sleep(0.05)

    assert after > before
    assert _job(db, job_id).heartbeat_at == after


# --- running jobs ---------------------------------------------------------------------------


def _work(db, stages, **kw) -> int:
    return work(
        db,
        poll_s=0,
        heartbeat_s=60,
        stale_after_s=STALE_S,
        once=True,
        stages=stages,
        make_context=fake_context,
        **kw,
    )


def test_run_job_runs_the_requested_stages(db) -> None:
    run_id = _run(db)
    stages = [FakeStage("a"), FakeStage("b"), FakeStage("c")]
    job_id = enqueue(db, run_id, {})
    with db.begin() as session:  # fake stage names: store the args directly
        session.get_one(Job, job_id).args = JobArgs(until="b").model_dump()
    job = claim_next(db, "w", STALE_S)

    done = run_job(db, job, heartbeat_s=60, stages=stages, make_context=fake_context)

    assert (done.status, done.error) == ("completed", None) and done.finished_at is not None
    assert [s.calls for s in stages] == [1, 1, 0]
    assert _run_status(db, run_id) == "stopped"


def test_a_failing_stage_fails_the_job_and_resume_is_accepted(db) -> None:
    run_id = _run(db)
    flaky = FakeStage("b", fail=RuntimeError)
    stages = [FakeStage("a"), flaky, FakeStage("c")]
    job_id = enqueue(db, run_id, {})

    assert _work(db, stages) == 1

    job = _job(db, job_id)
    assert (job.status, job.error) == ("failed", "RuntimeError: boom")
    assert [(r.stage, r.status) for r in _stage_runs(db, run_id)] == [
        ("a", "completed"),
        ("b", "failed"),
    ]
    assert _run_status(db, run_id) == "failed"

    resumed = enqueue(db, run_id, {"resume": True})
    flaky.fail = None
    _work(db, stages)
    assert _job(db, resumed).status == "completed"
    assert [s.calls for s in stages] == [1, 2, 1]
    assert _run_status(db, run_id) == "completed"


def test_invalid_options_fail_the_job(db) -> None:
    run_id = _run(db)
    enqueue(db, run_id, {"from_stage": "report"})  # not one of the fake stages

    _work(db, [FakeStage("a")])

    with db() as session:
        job = session.scalars(select(Job)).one()
    assert job.status == "failed" and "unknown stage 'report'" in job.error
    assert _run_status(db, run_id) == "planned"


def test_an_interrupted_worker_fails_the_job_and_cleans_up_the_run(db) -> None:
    run_id = _run(db)
    job_id = enqueue(db, run_id, {})
    lines: list[str] = []

    with pytest.raises(KeyboardInterrupt):
        _work(db, [FakeStage("a"), FakeStage("b", fail=KeyboardInterrupt)], log=lines.append)

    job = _job(db, job_id)
    assert (job.status, job.error) == ("failed", "interrupted")
    assert [(r.stage, r.status, r.error) for r in _stage_runs(db, run_id)] == [
        ("a", "completed", None),
        ("b", "failed", "interrupted"),
    ]
    assert _run_status(db, run_id) == "failed"
    assert lines[-1].endswith("interrupted")


def test_once_without_jobs_returns_immediately(db) -> None:
    assert _work(db, [FakeStage("a")]) == 0
