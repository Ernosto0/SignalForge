"""Stage I/O of ``competitors`` (agent-modules.md §5): seeds, page facts and the gap matrix.

Numbers the model uses to refer to things (competitors, claims, signals) are local to one prompt
input, never database ids, so cached answers survive a re-run that renumbers the rows.
"""

from typing import Literal

from pydantic import BaseModel, Field

FactKind = Literal["feature", "price", "segment", "integration", "limitation", "review_complaint"]
Period = Literal["month", "year", "one_time", "per_user_month", "per_document", "unknown"]
CellValue = Literal["yes", "partial", "no", "unknown"]


class CompetitorSeeds(BaseModel):
    """Output of ``competitor_seeds``: product names to search for (search seeds only)."""

    from_signals: list[str] = Field(description="products named in the evidence quotes")
    suggested: list[str] = Field(description="other products you know of; unverified")


class CompetitorFact(BaseModel):
    kind: FactKind
    quote: str = Field(description="copied verbatim from the page text")
    translation: str = Field(description="English translation of the quote")
    statement: str = Field(description="one English sentence: what the quote shows")
    amount: float | None = Field(default=None, description="price only: the number as stated")
    currency: str | None = Field(default=None, description="price only: ISO code, e.g. TRY")
    period: Period | None = None
    plan_name: str | None = None


class CompetitorPage(BaseModel):
    """Output of ``competitor_facts`` for one page."""

    competitor_name: str | None = Field(description="the product the page is about, or null")
    product_url: str | None = Field(description="the product's own website, if shown")
    page_kind: Literal["own_site", "review", "complaint", "comparison", "other"]
    segment: str | None = Field(description="who the product is for, as the page states it")
    geo: Literal["turkey", "international", "unknown"]
    facts: list[CompetitorFact]


class GapDimensionDraft(BaseModel):
    key: str = Field(description="snake_case, unique")
    label: str = Field(description="English, e.g. 'e-İrsaliye integration'")
    from_signals: list[int] = Field(description="numbers of the problem signals it comes from")


class GapCell(BaseModel):
    dimension: str  # dimension key
    competitor: int  # competitor number in the input
    value: CellValue
    claim: int | None = Field(description="number of the claim that shows the value")


class GapMatrixDraft(BaseModel):
    """Output of ``gap_matrix``: dimensions from the problem's signals, one cell per competitor."""

    dimensions: list[GapDimensionDraft]
    cells: list[GapCell]
