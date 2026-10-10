"""Opportunity scorer (agent-modules.md §8).

Job: for each opportunity, the three independent outputs of plan §8 (attractiveness 0–100,
confidence, founder fit), a rule-based category and the cheapest validation experiment, all
explainable from a rule trace and cited claims.

Stages: ``score`` (analysis tier: prompts/score.md for the rubric, prompts/founder_fit.md for
feasibility; entailment on the fast tier).
Reads: ``opportunities`` (Gate 2 status, buyer roles, economic model, channels, market breadth),
their clusters (evidence strength, counted signal facts), gap inferences and the competitor
facts behind them, competitor prices and segments, the request's ``founder``.
Writes: ``score_cards`` (``factors``, ``attractiveness``, ``confidence``, ``founder_fit``,
``category``, ``rule_trace``, ``experiment``); entailment verdicts on table facts
(``meta.entailment_by = "score"``). A knocked-out opportunity gets a ``weak`` card with no
factors, its knock-outs in the trace, and null attractiveness, confidence and founder fit.
Guards: the model judges five factors on the anchored rubric (scoring/rubric.yaml) citing local
claim numbers; code caps a factor without a cited fact, sets ``economic_impact`` and
``market_breadth``, applies the WTP floor, takes the median of k judges, and decides confidence,
founder fit, category and experiment by rules (scoring/scorer.py, categories.py, experiments.py).

Config blocks: ``score``. Done when (the M5 exit): each ScoreCard is fully explainable from its
rule trace and cited claims (:func:`check_score_cards`).
"""

from signalforge.agents.base import Agent
from signalforge.evidence.opportunities import check_score_cards
from signalforge.pipeline.stages.score import Score

OPPORTUNITY_SCORER = Agent(
    name="opportunity_scorer",
    description="Per opportunity: rubric factors, k judges, confidence, founder fit, category, "
    "experiment",
    stages=(Score(),),
)

__all__ = ["OPPORTUNITY_SCORER", "check_score_cards"]
