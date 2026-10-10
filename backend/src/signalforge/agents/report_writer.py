"""Report writer (agent-modules.md §9).

Job: the final research report: full reports for the top opportunities, a summary, a "Don't build"
section with reasons and an "insufficient evidence" list. The writer is constrained (plan §7.4):
it cites claim numbers only, and a validator rejects bullets that don't trace to their citations.

Stages: ``report`` (synthesis tier: prompts/report.md per section, prompts/report_summary.md once;
entailment on the fast tier).
Reads: score cards, opportunities (buyer roles, economic model, channels), clusters, competitors
and gap matrices through the score claim tables, and the claims with their excerpts and documents'
URLs.
Writes: ``<report.out_dir>/run-<id>/report.json`` (source of truth), ``report.md``,
``report.html``; entailment verdicts on the facts it cites (``meta.entailment_by = "report"``).
Full page text is never exported: excerpts are clipped to ``report.excerpt_max_chars``.
Guards: per-section claim tables; the pure validator (``reporting/validator.py``); regeneration of
a failing section, then dropping of failing bullets (``method.dropped_bullets``); the deterministic
sections are built in code from rule traces.

Config blocks: ``report``. Done when (the M6 exit): 100% of factual bullets are cited and the
validator catches seeded uncited or hallucinated bullets (:func:`check_report`).
"""

from signalforge.agents.base import Agent
from signalforge.evidence.reports import check_report
from signalforge.pipeline.stages.report import ReportStage

REPORT_WRITER = Agent(
    name="report_writer",
    description="Final report: constrained section writer, citation validator, deterministic "
    "don't-build and insufficient-evidence sections",
    stages=(ReportStage(),),
)

__all__ = ["REPORT_WRITER", "check_report"]
