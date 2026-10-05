from typing import Literal

from pydantic import BaseModel

from signalforge.domain.evidence import SignalType

QueryIntent = Literal["pain", "verify", "competitor", "regulatory", "jobs"]
# The intents query_gen owns; verify/competitor queries come from later stages.
GeneratedIntent = Literal["pain", "jobs", "regulatory"]


class QueryDraft(BaseModel):
    """One query proposed by the model; validated, deduplicated and capped before it is stored."""

    text: str
    intent: GeneratedIntent
    signal_type: SignalType
    source_hint: str | None  # a registry domain, turned into a `site:` operator


class QueryDrafts(BaseModel):
    """Output of the ``query_gen`` prompt for one submarket."""

    queries: list[QueryDraft]
