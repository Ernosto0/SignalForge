"""An agent is a named, ordered group of stages with one research job (agent-modules.md §0.1).

Agents are a layer over stages, not a replacement: the runner, StageRun bookkeeping, resume /
``--from``, the cache and the budget cap all work per stage. Running an agent runs its stages from
the first to the last, so it re-runs as a unit and replaces its own outputs.
"""

from dataclasses import dataclass

from signalforge.pipeline.runner import Stage


@dataclass(frozen=True)
class Agent:
    name: str  # module name, e.g. "problem_discovery"
    description: str  # one line, shown by `signalforge agents`
    stages: tuple[Stage, ...]  # run order within the agent

    @property
    def first(self) -> str:
        return self.stages[0].name

    @property
    def last(self) -> str:
        return self.stages[-1].name

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.stages)
