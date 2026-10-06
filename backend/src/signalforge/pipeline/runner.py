"""Run creation and per-stage bookkeeping (plan §4, §5).

Every stage is ``run(ctx) -> StageResult``; :func:`run_stage` wraps it in a ``StageRun`` row with
status, timings, cost (the run's spend delta) and metrics. :func:`run_pipeline` runs an ordered
stage list with ``--resume`` / ``--from`` / ``--until`` (order: ``pipeline.stages.STAGES``).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import Defaults
from signalforge.db.models import ResearchRun, StageRun
from signalforge.domain.plan import ResearchPlan
from signalforge.packs import MarketPack
from signalforge.pipeline.context import RunContext
from signalforge.providers.cache import cache_key
from signalforge.providers.llm import BudgetExceeded

# Run statuses a failed stage leaves behind; a later successful stage makes the run "stopped".
RESUMABLE_FAILURES = ("failed", "paused_budget")


@dataclass
class StageResult:
    metrics: dict[str, Any] = field(default_factory=dict)
    input_hash: str | None = None


class Stage(Protocol):
    name: str

    def run(self, ctx: RunContext) -> StageResult: ...


def create_run(
    db: sessionmaker[Session], plan: ResearchPlan, pack: MarketPack, defaults: Defaults
) -> int:
    """Store a run whose plan has passed the human checkpoint."""
    with db.begin() as session:
        run = ResearchRun(
            status="planned",
            request=plan.request.model_dump(mode="json"),
            plan=plan.model_dump(mode="json"),
            pack_id=pack.id,
            pack_version=pack.version,
            config_hash=cache_key(defaults.model_dump(mode="json")),
            budget_usd=Decimal(str(plan.request.budget_usd)),
        )
        session.add(run)
        session.flush()
        return run.id


def load_run_plan(ctx: RunContext) -> ResearchPlan:
    with ctx.db() as session:
        plan = session.scalar(select(ResearchRun.plan).where(ResearchRun.id == _run_id(ctx)))
    if plan is None:
        raise ValueError(f"run {ctx.run_id} has no plan")
    return ResearchPlan.model_validate(plan)


def run_stage(ctx: RunContext, stage: Stage) -> StageRun:
    """Run one stage and record it. Failures are recorded, then re-raised."""
    run_id = _run_id(ctx)
    with ctx.db.begin() as session:
        row = StageRun(run_id=run_id, stage=stage.name, status="running")
        session.add(row)
        session.flush()
        spent_before = _spent(session, run_id)

    try:
        result = stage.run(ctx)
    except Exception as exc:
        with ctx.db.begin() as session:
            row = session.merge(row)
            row.status = "failed"
            row.error = f"{type(exc).__name__}: {exc}"
            row.finished_at = datetime.now(UTC)
            row.cost_usd = _spent(session, run_id) - spent_before
            run = session.get_one(ResearchRun, run_id)
            run.status = "paused_budget" if isinstance(exc, BudgetExceeded) else "failed"
        raise

    with ctx.db.begin() as session:
        row = session.merge(row)
        row.status = "completed"
        row.input_hash = result.input_hash
        row.metrics = result.metrics
        row.finished_at = datetime.now(UTC)
        row.cost_usd = _spent(session, run_id) - spent_before
        run = session.get_one(ResearchRun, run_id)
        if run.status in RESUMABLE_FAILURES:
            # The stage that failed has now succeeded: the run is resumable again, not failed.
            # (run_pipeline sets its own statuses around the stages it runs.)
            run.status = "stopped"
    return row


def _run_id(ctx: RunContext) -> int:
    if ctx.run_id is None:
        raise ValueError("stage needs a RunContext created with a run_id")
    return ctx.run_id


def _spent(session: Session, run_id: int) -> Decimal:
    return session.scalar(select(ResearchRun.spent_usd).where(ResearchRun.id == run_id))


def latest_stage_runs(db: sessionmaker[Session], run_id: int) -> dict[str, StageRun]:
    """The most recent StageRun per stage name."""
    with db() as session:
        rows = session.scalars(
            select(StageRun).where(StageRun.run_id == run_id).order_by(StageRun.id)
        ).all()
    return {row.stage: row for row in rows}


def run_pipeline(
    ctx: RunContext,
    stages: Sequence[Stage],
    *,
    resume: bool = False,
    from_stage: str | None = None,
    until: str | None = None,
) -> list[StageRun]:
    """Run ``stages`` in order (plan §4).

    ``resume`` skips the leading stages whose latest StageRun completed; ``from_stage`` re-runs
    from that stage on (each stage replaces its own outputs), and requires every earlier stage to
    have completed; ``until`` stops after the named stage. The run is left ``stopped`` (resumable)
    when the requested stages finish before the end of the pipeline.
    """
    names = [s.name for s in stages]
    for name in (from_stage, until):
        if name is not None and name not in names:
            raise ValueError(f"unknown stage {name!r}; stages: {', '.join(names)}")
    run_id = _run_id(ctx)
    latest = latest_stage_runs(ctx.db, run_id)

    start = names.index(from_stage) if from_stage else 0
    if resume and not from_stage:
        while start < len(names) and _completed(latest, names[start]):
            start += 1
    if missing := [n for n in names[:start] if not _completed(latest, n)]:
        raise ValueError(f"earlier stages have not completed: {', '.join(missing)}")
    end = names.index(until) + 1 if until else len(names)

    with ctx.db.begin() as session:
        session.get_one(ResearchRun, run_id).status = "running"
    done = [run_stage(ctx, stage) for stage in stages[start:end]]
    with ctx.db.begin() as session:
        session.get_one(ResearchRun, run_id).status = (
            "completed" if end == len(names) else "stopped"
        )
    return done


def _completed(latest: dict[str, StageRun], name: str) -> bool:
    return name in latest and latest[name].status == "completed"
