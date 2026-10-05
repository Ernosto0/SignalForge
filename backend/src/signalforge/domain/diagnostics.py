from typing import Literal

from pydantic import BaseModel

SignalType = Literal[
    "complaint", "workaround", "labor_spend", "wish", "tool_complaint", "price_signal", "regulatory"
]


class PainCheck(BaseModel):
    """Output of the ``llm_check`` prompt: a one-call smoke test of the structured LLM path."""

    is_business_pain: bool
    signal_type: SignalType | None
    actor: str | None
    summary_en: str
