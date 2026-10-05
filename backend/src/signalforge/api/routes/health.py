from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from signalforge import __version__
from signalforge.config import get_settings
from signalforge.db.session import get_engine

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    database: bool
    llm_key_configured: bool
    search_key_configured: bool


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        database = True
    except SQLAlchemyError:
        database = False

    return HealthResponse(
        status="ok" if database else "degraded",
        version=__version__,
        database=database,
        llm_key_configured=settings.openai_api_key is not None,
        search_key_configured=settings.serp_api_key is not None,
    )
