"""Problem discovery (agent-modules.md §3).

Job: read every document, pull out typed signals backed by verbatim quotes, and group them into at
most ``cluster.max_clusters`` problem clusters: the problem landscape. Deciding which problems are
well evidenced is ``evidence_validator``'s job (Gate 1).

Stages:

- ``extract`` (fast tier, prompts/extract.md, one call per document chunk).
  Reads the run's documents (one per ``near_dup`` / ``syndicated`` group), page text from the page
  cache or the SERP snippet for snippet-only documents, the plan and the pack.
  Writes ``excerpts`` (verified ``exact`` / ``fuzzy``, offsets into the source text, salted author
  hash), ``signals`` (``meta.submarket`` = a plan submarket name), one ``fact`` claim per signal
  supported by its excerpt, and ``same_author`` independence groups.
  Guards: a quote not found in its chunk is dropped and counted; quotes outside the length bounds
  and overlapping quotes in one document are dropped; raw author names are never stored.

- ``cluster`` (analysis tier, prompts/cluster.md, cluster_assign.md, cluster_merge.md).
  Reads signals (id, type, submarket, actor, workflow, statement); never quotes.
  Writes ``problem_clusters`` (name, description, signal ids, independent sources, evidence
  strength, rank) and one ``inference`` claim per cluster derived from its signals' fact claims
  (``problem_clusters.claim_id``).
  Guards: every signal id ends up in exactly one cluster or in noise; unknown ids are dropped;
  repeated ids stay where most same-document signals are; missing ids get one repair call;
  overflow beyond ``max_clusters`` is dissolved; clusters under ``min_cluster_size`` go to noise
  (except a lone regulatory signal).

Config blocks: ``extract``, ``cluster``, ``strength``. Exit (M3): quote verification passes for
≥95% of kept excerpts, and ≥70% of 50 labelled signals are real first-hand B2B pain
(``signalforge label signals``).
"""

from signalforge.agents.base import Agent
from signalforge.pipeline.stages.cluster import Cluster
from signalforge.pipeline.stages.extract import Extract

PROBLEM_DISCOVERY = Agent(
    name="problem_discovery",
    description="Documents → verified signals with fact claims → problem clusters (landscape)",
    stages=(Extract(), Cluster()),
)
