from typing import Literal

from pydantic import BaseModel, Field

# What the page most likely is, judged from title + snippet + domain (prompts/triage.md).
TriageLabel = Literal[
    "first_hand_pain",  # practitioners/companies describing their own operational problems
    "tool_review",  # business users reviewing or complaining about a product / vendor
    "job_ad",  # job posting whose duties reveal manual work
    "regulatory",  # official text or authoritative explanation of an obligation / deadline
    "industry_news",  # trade press / association report on sector problems
    "vendor_marketing",
    "seo_content",  # listicles, generic guides, "X nedir"
    "consumer",  # end-customer complaints or questions
    "job_seeker",  # careers, salaries, interview questions
    "off_topic",
]


class TriageJudgment(BaseModel):
    id: int
    label: TriageLabel
    score: int = Field(ge=0, le=3)
    reason: str  # English, ≤ 15 words


class TriageBatch(BaseModel):
    """Output of the ``triage`` prompt for one batch of URLs."""

    judgments: list[TriageJudgment]
