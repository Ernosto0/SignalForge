"""Monetization (agent-modules.md §7).

Job: for each opportunity, estimate what solving the problem is worth to one customer per month
as a range over explicit, dated assumptions; collect willingness-to-pay signals; knock out
opportunities that cannot pay (Gate 2, plan §4).

Stages: ``monetization`` (analysis tier, prompts/monetization.md, one call per opportunity).
Reads: ``opportunities`` from ``buyers`` (problem still shortlisted), the problem's counted signal
facts (``labor_spend`` job ads, ``price_signal``), competitor ``pricing`` and price facts, the
pack's ``economics.yaml`` (dated wages, working hours, ``usd_try``), the request's
``founder.min_customer_value_usd_month``.
Writes: ``Opportunity.economic_model``, ``wtp_signals``, ``status`` (``passed`` |
``knocked_out``), ``knockouts``; ``assumption`` claims (stage ``monetization``) with ``meta``
``{name, value_low, value_high, unit, currency, as_of, source_url, sourced, pack_reference,
derivation?}``.
Guards: code computes every number (scoring/economics.py). A cited pack reference supplies the
value, not the model; wages become hourly employer cost with the config's loaded-cost multiplier.
Units must match the formula, ranges must be ordered and non-negative, fractions ≤ 1; one bad
input invalidates the model. Only pack values are ``sourced``; model estimates are
``sourced: false`` with their citations as context. A missing
``usd_try`` pack entry fails the stage.

Config blocks: ``monetization``. Done when (part of the M5 exit): every number in an economic
model traces to an assumption claim with a source or an "unsourced" label
(:func:`check_economic_models`).
"""

from signalforge.agents.base import Agent
from signalforge.evidence.opportunities import check_economic_models
from signalforge.pipeline.stages.monetization import Monetization

MONETIZATION = Agent(
    name="monetization",
    description="Per opportunity: value range over dated assumptions, WTP signals, Gate 2",
    stages=(Monetization(),),
)

__all__ = ["MONETIZATION", "check_economic_models"]
