from collections import defaultdict
from collections.abc import Sequence
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, sessionmaker

from signalforge.agents import AGENTS, STAGES, Agent
from signalforge.api.deps import DB, Config
from signalforge.api.schemas import (
    AgentProgress,
    AgentStatus,
    JobOut,
    RunDetail,
    RunSummary,
    StageOut,
)
from signalforge.config import Defaults
from signalforge.db.models import Job, Opportunity, ResearchRun, StageRun
from signalforge.domain.plan import ResearchPlan
from signalforge.packs import load_pack
from signalforge.pipeline.jobs import JobArgs, JobConflict, enqueue
from signalforge.pipeline.runner import create_run
from signalforge.reporting.render import report_dir

router = APIRouter(tags=["runs"])

REPORT_TYPES = {
    "json": "application/json",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


@router.post("/runs", response_model=RunDetail, status_code=201)
def create(plan: ResearchPlan, db: DB, config: Config, start: bool = True) -> RunDetail:
    """Store a reviewed plan as a new run; queue a full-pipeline job unless ``start=false``."""
    try:
        pack = load_pack(plan.request.pack_id)
    except FileNotFoundError as exc:
        raise _invalid(["body", "request", "country"], str(exc)) from exc
    run_id = create_run(db, plan, pack, config)
    if start:
        enqueue(db, run_id, JobArgs())
    return _detail(db, config, run_id)


@router.get("/runs", response_model=list[RunSummary])
def list_runs(db: DB, limit: int = Query(50, ge=1, le=500)) -> list[RunSummary]:
    """Newest first."""
    with db() as session:
        runs = session.scalars(
            select(ResearchRun).order_by(ResearchRun.id.desc()).limit(limit)
        ).all()
        return _summaries(session, runs)


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: int, db: DB, config: Config) -> RunDetail:
    return _detail(db, config, run_id)


@router.post("/runs/{run_id}/jobs", response_model=JobOut, status_code=201)
def create_job(run_id: int, args: JobArgs, db: DB) -> JobOut:
    """Queue a pipeline job: resume, re-run from a stage, stop after a stage, or re-run an agent."""
    try:
        job_id = enqueue(db, run_id, args)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise _invalid(["body"], str(exc)) from exc
    with db() as session:
        return JobOut.model_validate(session.get_one(Job, job_id), from_attributes=True)


@router.get("/runs/{run_id}/report/{fmt}", response_class=FileResponse)
def get_report(run_id: int, fmt: Literal["json", "md", "html"], config: Config) -> FileResponse:
    """A report file written by the ``report`` stage."""
    path = report_dir(config.report, run_id) / f"report.{fmt}"
    if not path.is_file():
        raise HTTPException(404, f"run {run_id} has no {fmt} report")
    return FileResponse(path, media_type=REPORT_TYPES[fmt])


def _invalid(loc: list[str], msg: str) -> RequestValidationError:
    """A 422 in FastAPI's own validation-error shape, so clients handle one format."""
    return RequestValidationError([{"loc": loc, "msg": msg, "type": "value_error"}])


def _detail(db: sessionmaker[Session], config: Defaults, run_id: int) -> RunDetail:
    with db() as session:
        run = session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        (summary,) = _summaries(session, [run])
        jobs = session.scalars(
            select(Job).where(Job.run_id == run_id).order_by(Job.id.desc())
        ).all()
        latest = _latest_stage_runs(session, [run_id])[run_id]
    directory = report_dir(config.report, run_id)
    return RunDetail(
        **summary.model_dump(),
        plan=run.plan,
        pack_id=run.pack_id,
        pack_version=run.pack_version,
        agents=[_agent_progress(agent, latest) for agent in AGENTS],
        jobs=[JobOut.model_validate(j, from_attributes=True) for j in jobs],
        report_formats=[f for f in REPORT_TYPES if (directory / f"report.{f}").is_file()],
    )


def _summaries(session: Session, runs: Sequence[ResearchRun]) -> list[RunSummary]:
    ids = [r.id for r in runs]
    latest = _latest_stage_runs(session, ids)
    jobs = {
        j.run_id: j
        for j in session.scalars(
            select(Job)
            .where(Job.run_id.in_(ids))
            .ext(distinct_on(Job.run_id))
            .order_by(Job.run_id, Job.id.desc())
        )
    }
    opportunities = dict(
        session.execute(
            select(Opportunity.run_id, func.count())
            .where(Opportunity.run_id.in_(ids))
            .group_by(Opportunity.run_id)
        ).all()
    )
    names = [s.name for s in STAGES]
    summaries = []
    for run in runs:
        stages = latest[run.id]
        running = [r for r in stages.values() if r.status == "running"]
        job = jobs.get(run.id)
        summaries.append(
            RunSummary(
                id=run.id,
                created_at=run.created_at,
                updated_at=run.updated_at,
                status=run.status,
                country=run.request.get("country", ""),
                industry=run.request.get("industry", ""),
                problem_area=run.request.get("problem_area"),
                budget_usd=run.budget_usd,
                spent_usd=run.spent_usd,
                current_stage=max(running, key=lambda r: r.id).stage if running else None,
                stages_done=sum(
                    1 for n in names if n in stages and stages[n].status == "completed"
                ),
                stages_total=len(names),
                latest_job=JobOut.model_validate(job, from_attributes=True) if job else None,
                opportunity_count=opportunities.get(run.id, 0),
            )
        )
    return summaries


def _latest_stage_runs(session: Session, run_ids: list[int]) -> dict[int, dict[str, StageRun]]:
    """The most recent StageRun per (run, stage)."""
    latest: dict[int, dict[str, StageRun]] = defaultdict(dict)
    for row in session.scalars(
        select(StageRun)
        .where(StageRun.run_id.in_(run_ids))
        .ext(distinct_on(StageRun.run_id, StageRun.stage))
        .order_by(StageRun.run_id, StageRun.stage, StageRun.id.desc())
    ):
        latest[row.run_id][row.stage] = row
    return latest


def _agent_progress(agent: Agent, latest: dict[str, StageRun]) -> AgentProgress:
    stages = [_stage_out(name, latest.get(name)) for name in agent.stage_names]
    statuses = {s.status for s in stages}
    status: AgentStatus
    if statuses == {"completed"}:
        status = "completed"
    elif "running" in statuses:
        status = "running"
    elif "failed" in statuses:
        status = "failed"
    elif "completed" in statuses:
        status = "partial"
    else:
        status = "pending"
    started = [s.started_at for s in stages if s.started_at]
    finished = [s.finished_at for s in stages if s.finished_at]
    return AgentProgress(
        name=agent.name,
        description=agent.description,
        status=status,
        cost_usd=sum(s.cost_usd for s in stages),
        started_at=min(started, default=None),
        finished_at=max(finished, default=None) if status in ("completed", "failed") else None,
        stages=stages,
    )


def _stage_out(name: str, row: StageRun | None) -> StageOut:
    if row is None:
        return StageOut(
            name=name,
            status="pending",
            started_at=None,
            finished_at=None,
            cost_usd=0,
            error=None,
            metrics={},
        )
    return StageOut(
        name=name,
        # "skipped" is reserved in the model but never written; show it as done.
        status="completed" if row.status == "skipped" else row.status,  # type: ignore[arg-type]
        started_at=row.started_at,
        finished_at=row.finished_at,
        cost_usd=row.cost_usd,
        error=row.error,
        metrics=row.metrics or {},
    )
