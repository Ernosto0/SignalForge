from pydantic import BaseModel

from signalforge.domain.evidence import SignalType


class PainCheck(BaseModel):
    """Output of the ``llm_check`` prompt: a one-call smoke test of the structured LLM path."""

    is_business_pain: bool
    signal_type: SignalType | None
    actor: str | None
    summary_en: str
