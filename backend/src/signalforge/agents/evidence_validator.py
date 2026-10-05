"""Evidence validator (agent-modules.md §4).

Job: decide which problems have enough independent, good-quality evidence to research further.
Part 1 (M3) is Gate 1, deterministic: ``shortlist``. Part 2 (M4) adds ``verify`` (bounded search
loop + entailment) after it.

Stages: ``shortlist``.
Reads: ``problem_clusters`` with their independent source counts and evidence strength.
Writes: per cluster ``shortlisted`` and ``gate_trace`` (which rule decided, with its values).
"""

from signalforge.agents.base import Agent
from signalforge.pipeline.stages.shortlist import Shortlist

EVIDENCE_VALIDATOR = Agent(
    name="evidence_validator",
    description="Gate 1: shortlist problems with enough independent evidence",
    stages=(Shortlist(),),
)
