from fastapi import APIRouter, HTTPException

from signalforge.api.deps import DB
from signalforge.api.schemas import JobOut
from signalforge.pipeline.jobs import JobConflict, cancel

router = APIRouter(tags=["jobs"])


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: int, db: DB) -> JobOut:
    """Cancel a queued job (409 once a worker has claimed it)."""
    try:
        job = cancel(db, job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return JobOut.model_validate(job, from_attributes=True)
