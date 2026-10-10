"""Buyer research (agent-modules.md §6).

Job: for each shortlisted problem, decide who would buy a solution: 1–3 opportunities (segment ×
solution angle), each naming its user, buyer, decision maker, economic beneficiary and budget
owner, and the channels that reach them.

Stages: ``buyers`` (analysis tier, prompts/buyers.md, one call per problem).
Reads: shortlisted clusters with their counted signal facts (extract + verify, failed entailment
out; ``actor`` and ``labor_spend`` job ads name roles), the cluster inference, gap inferences and
competitor ``segment`` facts (stage ``competitors``), the plan's ``buyer_hypotheses`` and
submarket ``actors``, and the request's ``target_customer``.
Writes: ``opportunities`` (``segment``, ``solution_angle``, ``buyer_roles``, ``accessibility``,
``market_breadth`` with ``status: not_searched``), hypothesis claims (stage ``buyers``) for the
plan's buyer hypotheses (``meta.origin = "plan"``) and for each role hypothesis
(``meta.opportunity_id``, ``meta.role``).
Guards: the model cites local claim numbers only; unknown numbers are dropped and counted. A role
with neither a citation nor a hypothesis drops its opportunity; a null budget owner is kept for
Gate 2. Gap citations must be the problem's own gap inferences. Near-duplicate segments collapse,
then the list is capped. One problem's failed call leaves it without opportunities, counted.

Config blocks: ``buyers``. Done when (part of the M5 exit): every opportunity's buyer roles trace
to cited claims of the run or to labelled hypothesis claims (:func:`check_buyer_roles`).
"""

from signalforge.agents.base import Agent
from signalforge.evidence.opportunities import check_buyer_roles
from signalforge.pipeline.stages.buyers import Buyers

BUYER_RESEARCH = Agent(
    name="buyer_research",
    description="Per shortlisted problem: segments, solution angles and cited buyer roles",
    stages=(Buyers(),),
)

__all__ = ["BUYER_RESEARCH", "check_buyer_roles"]
