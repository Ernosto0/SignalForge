"""Source discovery (agent-modules.md §2).

Job: find and download the pages most likely to hold first-hand evidence for the plan.

Stages: ``query_gen`` → ``search`` → ``triage`` → ``fetch`` → ``dedupe``.
Reads: ``ResearchRun.plan``, the market pack, config blocks ``query_gen``, ``collect``, ``triage``,
``fetch_stage``, ``dedupe``.
Writes: ``queries``, ``search_results``, ``url_candidates``, ``documents`` (full text in the page
cache), ``independence_groups`` (``near_dup`` / ``syndicated``).
"""

from signalforge.agents.base import Agent
from signalforge.pipeline.stages.dedupe import Dedupe
from signalforge.pipeline.stages.fetch import Fetch
from signalforge.pipeline.stages.query_gen import QueryGen
from signalforge.pipeline.stages.search import Search
from signalforge.pipeline.stages.triage import Triage

SOURCE_DISCOVERY = Agent(
    name="source_discovery",
    description="Plan → Turkish queries → SERP → triaged URLs → fetched, de-duplicated documents",
    stages=(QueryGen(), Search(), Triage(), Fetch(), Dedupe()),
)
