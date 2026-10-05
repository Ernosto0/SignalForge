from typing import Literal

from pydantic import BaseModel

# Signal types (plan §5). Each type supports a different kind of claim.
SignalType = Literal[
    "complaint", "workaround", "labor_spend", "wish", "tool_complaint", "price_signal", "regulatory"
]


class ExtractedSignal(BaseModel):
    """One signal proposed by the ``extract`` prompt. The quote is verified before it is stored."""

    quote: str  # verbatim from the text, original language
    translation: str  # English
    type: SignalType
    actor: str | None  # role / company type as stated, e.g. "nakliye firması sahibi"
    workflow: str | None  # the business process concerned, English
    statement: str  # one English sentence: who has what problem
    first_hand: bool  # the writer describes their own work / company
    # One of the plan's submarket names, or null. The default only serves tests: the strict output
    # schema sent to the model still requires the field.
    submarket: str | None = None
    author: str | None  # display name shown with the quote, if any (hashed, never stored)


class ExtractionBatch(BaseModel):
    """Output of the ``extract`` prompt for one chunk of one document."""

    signals: list[ExtractedSignal]


class ClusterDraft(BaseModel):
    name: str  # English, ≤ 8 words
    description: str  # English, 1–2 sentences: who, which workflow, what goes wrong
    signal_ids: list[int]


class ClusterBatch(BaseModel):
    """Output of the ``cluster`` prompt: problem clusters plus signals that fit none."""

    clusters: list[ClusterDraft]
    noise: list[int]


class ClusterAssignment(BaseModel):
    signal_id: int
    cluster: int | None  # index into the given clusters; null = noise


class AssignmentBatch(BaseModel):
    """Output of the ``cluster_assign`` prompt: leftover signals placed into existing clusters."""

    assignments: list[ClusterAssignment]


class ClusterMerge(BaseModel):
    name: str
    description: str
    members: list[int]  # ids of the chunk clusters merged into this one


class MergeBatch(BaseModel):
    """Output of the ``cluster_merge`` prompt: chunk clusters combined into run-level clusters."""

    clusters: list[ClusterMerge]
