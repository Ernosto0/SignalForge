from fastapi import APIRouter

from signalforge.agents import AGENTS
from signalforge.api.schemas import AgentOut

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=list[AgentOut])
def list_agents() -> list[AgentOut]:
    """The research agents and their stages, in run order."""
    return [
        AgentOut(name=a.name, description=a.description, stages=list(a.stage_names)) for a in AGENTS
    ]
