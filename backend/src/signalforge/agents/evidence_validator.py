"""Evidence validator (agent-modules.md §4).

Job: decide which problems have enough independent, good-quality evidence to research further,
then strengthen or weaken each shortlisted problem with a targeted second round, and check that its
key facts are really supported by their excerpts.

Stages: ``shortlist`` (Gate 1, deterministic) → ``verify`` (bounded search loop, entailment,
Gate-1 re-check).
Reads: ``problem_clusters`` (independent source counts, evidence strength), their signals,
excerpts and documents, the plan and the pack's source registry.
Writes:
- shortlist: per cluster ``shortlisted`` and ``gate_trace`` (which rule decided, with its values).
- verify: ``queries`` (intent ``verify``), ``documents`` (origin ``verify``), excerpts / signals /
  fact claims (stage ``verify``; counter-evidence flagged in ``Signal.meta.counter``), independence
  groups involving its documents, ``Claim.entailment`` verdicts, and per cluster
  ``verification`` plus ``verify_*`` gate-trace entries; a problem failing the re-check is no
  longer shortlisted.

Exports :func:`entail_pending` (evidence/entailment.py) for later agents (report_writer).
"""

from signalforge.agents.base import Agent
from signalforge.evidence.entailment import entail_pending
from signalforge.pipeline.stages.shortlist import Shortlist
from signalforge.pipeline.stages.verify import Verify

EVIDENCE_VALIDATOR = Agent(
    name="evidence_validator",
    description="Gate 1 shortlist, then second-round search, entailment and a Gate-1 re-check",
    stages=(Shortlist(), Verify()),
)

__all__ = ["EVIDENCE_VALIDATOR", "entail_pending"]
