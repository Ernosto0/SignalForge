"""Research agents (agent-modules.md §0): named groups of pipeline stages, one per research job.

``AGENTS``, ``STAGES`` and ``get_agent`` live in :mod:`signalforge.agents.registry` and are loaded
lazily: stage modules import shared agent code (``agents/loop.py``), and the agent modules import
the stages, so importing the registry eagerly here would be circular.
"""

from typing import Any

from signalforge.agents.base import Agent


def __getattr__(name: str) -> Any:
    if name in ("AGENTS", "STAGES", "get_agent"):
        from signalforge.agents import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["AGENTS", "STAGES", "Agent", "get_agent"]
