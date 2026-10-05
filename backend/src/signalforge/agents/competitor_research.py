"""Competitor research (agent-modules.md §5).

Job: for each shortlisted problem, find the products that already address it, record what they do
and what they cost as cited facts from their own pages and reviews, and build a gap matrix showing
where they are weak. Competition is not automatically bad (plan §1).

Stages: ``competitors``.
Reads: shortlisted clusters with their counted signals (extract + verify, failed facts excluded;
``tool_complaint`` quotes name vendors), the pack registry (``complaints``, ``reviews``,
``forum`` domains as `site:` hints).
Writes: ``queries`` (intent ``competitor``), ``documents`` (origin ``competitor``), excerpts and
fact claims (stage ``competitors``, ``meta.competitor_id``), ``competitors`` with dated
``pricing``, ``gap_matrices`` (every cell a cited fact or ``unknown``), and one gap inference claim
per uncovered dimension.
"""

from signalforge.agents.base import Agent
from signalforge.pipeline.stages.competitors import Competitors

COMPETITOR_RESEARCH = Agent(
    name="competitor_research",
    description="Existing products per shortlisted problem → cited facts and prices → gap matrix",
    stages=(Competitors(),),
)
