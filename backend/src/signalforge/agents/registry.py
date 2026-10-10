"""The research agents in run order (agent-modules.md §0). Later agents are appended as built.

``planner`` is not here: it runs before a run exists (human checkpoint). The pipeline's stage
order is derived from this tuple, so there is one source of truth for ordering.
"""

from signalforge.agents.base import Agent
from signalforge.agents.buyer_research import BUYER_RESEARCH
from signalforge.agents.competitor_research import COMPETITOR_RESEARCH
from signalforge.agents.evidence_validator import EVIDENCE_VALIDATOR
from signalforge.agents.monetization import MONETIZATION
from signalforge.agents.opportunity_scorer import OPPORTUNITY_SCORER
from signalforge.agents.problem_discovery import PROBLEM_DISCOVERY
from signalforge.agents.source_discovery import SOURCE_DISCOVERY

AGENTS: tuple[Agent, ...] = (
    SOURCE_DISCOVERY,
    PROBLEM_DISCOVERY,
    EVIDENCE_VALIDATOR,
    COMPETITOR_RESEARCH,
    BUYER_RESEARCH,
    MONETIZATION,
    OPPORTUNITY_SCORER,
)

STAGES = tuple(stage for agent in AGENTS for stage in agent.stages)


def get_agent(name: str) -> Agent:
    for agent in AGENTS:
        if agent.name == name:
            return agent
    raise ValueError(f"unknown agent {name!r}; agents: {', '.join(a.name for a in AGENTS)}")
