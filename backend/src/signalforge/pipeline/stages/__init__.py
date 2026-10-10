"""Pipeline stages (plan §4). Their run order, ``STAGES``, is derived from the agents
(``signalforge.agents.AGENTS``), which group the stages by research job.

Document-level dedupe runs before extract, so only one copy of duplicated text is read; author
grouping (``same_author``) happens in extract once authors are known.
"""

from typing import Any

from signalforge.pipeline.stages.buyers import Buyers
from signalforge.pipeline.stages.cluster import Cluster
from signalforge.pipeline.stages.competitors import Competitors
from signalforge.pipeline.stages.dedupe import Dedupe
from signalforge.pipeline.stages.extract import Extract
from signalforge.pipeline.stages.fetch import Fetch
from signalforge.pipeline.stages.monetization import Monetization
from signalforge.pipeline.stages.query_gen import QueryGen
from signalforge.pipeline.stages.search import Search
from signalforge.pipeline.stages.shortlist import Shortlist
from signalforge.pipeline.stages.triage import Triage
from signalforge.pipeline.stages.verify import Verify


def __getattr__(name: str) -> Any:
    # Lazy: agent modules import the stage modules above, so importing them here eagerly would be
    # circular.
    if name == "STAGES":
        from signalforge.agents.registry import STAGES

        return STAGES
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "STAGES", "Buyers", "Cluster", "Competitors", "Dedupe", "Extract", "Fetch", "Monetization",
    "QueryGen", "Search", "Shortlist", "Triage", "Verify",
]  # fmt: skip
