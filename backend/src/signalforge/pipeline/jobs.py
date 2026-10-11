"""Job queue for pipeline runs (plan §11 item 11): a Postgres table, no Redis.

The API (or anything else) enqueues a job per run; ``signalforge worker`` claims the oldest queued
job with ``SELECT … FOR UPDATE SKIP LOCKED`` and runs it with :func:`run_pipeline`. A partial
unique index keeps at most one queued/running job per run. While a job runs, a thread updates its
``heartbeat_at``; a running job whose heartbeat is older than ``jobs.stale_after_s`` is failed
("worker lost") on the next claim, and its run is left ``failed`` (resumable).
"""

import os
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from signalforge.agents.registry import STAGES, get_agent
from signalforge.config import CacheMode
from signalforge.db.models import Job, ResearchRun, StageRun
from signalforge.pipeline.context import RunContext
from signalforge.pipeline.runner import Stage, run_pipeline

ACTIVE = ("queued", "running")

ContextFactory = Callable[[sessionmaker[Session], str, int], RunContext]


class JobConflict(Exception):
    """The run already has an active job, or the job is not in a state that allows the action."""


class JobArgs(BaseModel):
    """The CLI ``run`` options, as a job stores them."""

    model_config = ConfigDict(extra="forbid")

    resume: bool = False
    from_stage: str | None = None
    until: str | None = None
    agent: str | None = None


def pipeline_options(args: JobArgs, stages: Sequence[Stage] = STAGES) -> dict[str, Any]:
    """``run_pipeline`` keyword arguments for ``args``; raises ``ValueError`` on invalid ones.

    ``agent`` re-runs one agent's stages as a unit, so it excludes the other options.
    """
    if args.agent is not None:
        if args.from_stage or args.until or args.resume:
            raise ValueError("agent cannot be combined with from_stage, until or resume")
        chosen = get_agent(args.agent)
        return {"resume": False, "from_stage": chosen.first, "until": chosen.last}
    names = [s.name for s in stages]
    for name in (args.from_stage, args.until):
        if name is not None and name not in names:
            raise ValueError(f"unknown stage {name!r}; stages: {', '.join(names)}")
    if args.from_stage and args.until and names.index(args.until) < names.index(args.from_stage):
        raise ValueError(f"until {args.until!r} comes before from_stage {args.from_stage!r}")
    return {"resume": args.resume, "from_stage": args.from_stage, "until": args.until}


def enqueue(db: sessionmaker[Session], run_id: int, args: JobArgs | dict[str, Any]) -> int:
    """Queue a job for ``run_id``. Raises ``ValueError`` on invalid args, ``LookupError`` for an
    unknown run and :class:`JobConflict` when the run already has a queued or running job."""
    args = JobArgs.model_validate(args)
    pipeline_options(args)
    try:
        with db.begin() as session:
            if session.get(ResearchRun, run_id) is None:
                raise LookupError(f"run {run_id} not found")
            active = session.scalar(
                select(Job).where(Job.run_id == run_id, Job.status.in_(ACTIVE)).limit(1)
            )
            if active is not None:
                raise JobConflict(f"run {run_id} already has a {active.status} job (#{active.id})")
            job = Job(run_id=run_id, status="queued", args=args.model_dump())
            session.add(job)
            session.flush()
            return job.id
    except IntegrityError as exc:
        # A concurrent enqueue won the race for the partial unique index.
        if "uq_jobs_run_id_active" in str(exc.orig):
            raise JobConflict(f"run {run_id} already has an active job") from exc
        raise


def cancel(db: sessionmaker[Session], job_id: int) -> Job:
    """Cancel a queued job. A running job cannot be cancelled (stages are not interruptible)."""
    with db.begin() as session:
        job = session.get(Job, job_id, with_for_update=True)
        if job is None:
            raise LookupError(f"job {job_id} not found")
        if job.status != "queued":
            raise JobConflict(f"job {job_id} is {job.status}; only queued jobs can be cancelled")
        job.status = "cancelled"
        job.finished_at = datetime.now(UTC)
        return job


def claim_next(db: sessionmaker[Session], worker_id: str, stale_after_s: float) -> Job | None:
    """Fail stale running jobs, then claim the oldest queued job (``None`` if there is none)."""
    reclaim_stale(db, stale_after_s)
    with db.begin() as session:
        job = session.scalar(
            select(Job)
            .where(Job.status == "queued")
            .order_by(Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        now = datetime.now(UTC)
        job.status = "running"
        job.worker_id = worker_id
        job.started_at = now
        job.heartbeat_at = now
        return job


def reclaim_stale(db: sessionmaker[Session], stale_after_s: float) -> list[int]:
    """Fail running jobs whose worker stopped heart-beating; returns their ids."""
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_s)
    with db.begin() as session:
        jobs = session.scalars(
            select(Job)
            .where(Job.status == "running", Job.heartbeat_at < cutoff)
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            _abandon(session, job, "worker lost")
        return [job.id for job in jobs]


def abandon(db: sessionmaker[Session], job_id: int, reason: str) -> None:
    """Fail a running job that will not finish (worker interrupted) and clean up its run."""
    with db.begin() as session:
        job = session.get(Job, job_id, with_for_update=True)
        if job is not None and job.status == "running":
            _abandon(session, job, reason)


def _abandon(session: Session, job: Job, reason: str) -> None:
    # The stage that was running never recorded its end: fail it too, or the run would show it as
    # running forever and `resume` / `from_stage` would not see it as failed.
    now = datetime.now(UTC)
    job.status, job.error, job.finished_at = "failed", reason, now
    session.execute(
        update(StageRun)
        .where(StageRun.run_id == job.run_id, StageRun.status == "running")
        .values(status="failed", error=reason, finished_at=now)
    )
    session.execute(update(ResearchRun).where(ResearchRun.id == job.run_id).values(status="failed"))


@contextmanager
def heartbeat(db: sessionmaker[Session], job_id: int, interval_s: float) -> Iterator[None]:
    """Update the job's ``heartbeat_at`` every ``interval_s`` from a thread (own session)."""
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval_s):
            try:
                with db.begin() as session:
                    session.execute(
                        update(Job).where(Job.id == job_id).values(heartbeat_at=datetime.now(UTC))
                    )
            except SQLAlchemyError:
                pass  # the next beat retries; staleness is judged over minutes

    thread = threading.Thread(target=beat, name=f"job-{job_id}-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def default_context(cache_mode: CacheMode | None = None) -> ContextFactory:
    def make(db: sessionmaker[Session], pack_id: str, run_id: int) -> RunContext:
        return RunContext.create(cache_mode=cache_mode, pack_id=pack_id, run_id=run_id, db=db)

    return make


def run_job(
    db: sessionmaker[Session],
    job: Job,
    *,
    heartbeat_s: float,
    stages: Sequence[Stage] = STAGES,
    make_context: ContextFactory | None = None,
) -> Job:
    """Run a claimed job's pipeline and record its outcome. Stage failures (and invalid options)
    fail the job with the error text; the run's status is what the runner left behind."""
    make_context = make_context or default_context()
    error = None
    try:
        with heartbeat(db, job.id, heartbeat_s):
            with db() as session:
                pack_id = session.scalars(
                    select(ResearchRun.pack_id).where(ResearchRun.id == job.run_id)
                ).one()
            options = pipeline_options(JobArgs.model_validate(job.args), stages)
            run_pipeline(make_context(db, pack_id, job.run_id), stages, **options)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    with db.begin() as session:
        row = session.get_one(Job, job.id)
        row.status = "failed" if error else "completed"
        row.error = error
        row.finished_at = datetime.now(UTC)
        return row


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def work(
    db: sessionmaker[Session],
    *,
    poll_s: float,
    heartbeat_s: float,
    stale_after_s: float,
    once: bool = False,
    stages: Sequence[Stage] = STAGES,
    make_context: ContextFactory | None = None,
    log: Callable[[str], None] = lambda _: None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Claim and run jobs until interrupted (or one claim with ``once``); returns jobs run.

    ``KeyboardInterrupt`` during a job fails it ("interrupted") and cleans up its run, then
    propagates.
    """
    me = worker_id()
    done = 0
    while True:
        job = claim_next(db, me, stale_after_s)
        if job is None:
            if once:
                return done
            sleep(poll_s)
            continue
        log(f"job #{job.id} (run #{job.run_id}) started: {job.args}")
        try:
            job = run_job(
                db, job, heartbeat_s=heartbeat_s, stages=stages, make_context=make_context
            )
        except KeyboardInterrupt:
            abandon(db, job.id, "interrupted")
            log(f"job #{job.id} (run #{job.run_id}) interrupted")
            raise
        error = f": {job.error}" if job.error else ""
        log(f"job #{job.id} (run #{job.run_id}) {job.status}{error}")
        done += 1
        if once:
            return done
