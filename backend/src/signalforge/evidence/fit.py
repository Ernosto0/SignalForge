"""Software fit: could a small team's AI automation or workflow web app address a signal?

The extract prompt states four facts per signal (domain/evidence.py): whether the work recurs,
what is done by hand, what data it handles, and where the problem comes from. They are stored in
``Signal.meta["fit"]``. This module turns them into a yes/no with a configurable rule
(``software_fit`` in config/defaults.yaml), applied when the facts are read, so the rule can be
re-tuned without re-extracting.

A signal fits when its work recurs, handles documents, messages or data (not goods, vehicles or
devices), and its cause is something software can change: the business's own process, a weak
tool, or an obligation that creates paperwork. A third party failing (a carrier losing a parcel)
or broken hardware does not fit, however real the pain.
"""

from collections.abc import Mapping
from typing import Any

from signalforge.config import SoftwareFitDefaults
from signalforge.domain.evidence import ExtractedSignal


def fit_facts(signal: ExtractedSignal) -> dict[str, Any]:
    """The facts stored with a signal (``Signal.meta["fit"]``)."""
    return {
        "recurring": signal.recurring,
        "manual_task": signal.manual_task,
        "data_kind": signal.data_kind,
        "cause": signal.cause,
    }


def software_fit(facts: Mapping[str, Any] | None, cfg: SoftwareFitDefaults) -> bool:
    """Whether a signal with these facts fits; ``False`` for signals stored before the facts
    existed (``None``)."""
    if not facts:
        return False
    if cfg.require_recurring and not facts.get("recurring"):
        return False
    return facts.get("data_kind") in cfg.data_kinds and facts.get("cause") in cfg.causes
