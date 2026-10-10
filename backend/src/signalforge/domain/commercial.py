"""Stage I/O of ``buyers`` (agent-modules.md §6): opportunities with cited buyer roles.

Claim numbers the model cites are local to one prompt's claim table (1..n), never database ids,
so cached answers survive a re-run that renumbers the claims; the stage maps them back.
"""

from typing import Literal

from pydantic import BaseModel, Field

ChannelKind = Literal["association", "directory", "community", "marketplace", "event", "other"]


class RoleClaim(BaseModel):
    role: str = Field(description="as named in the market, e.g. 'operasyon müdürü'")
    claim_ids: list[int] = Field(
        description="numbers of table claims that name this role in this function"
    )
    hypothesis: str | None = Field(
        default=None, description="English; required when claim_ids is empty"
    )


class BuyerRoles(BaseModel):
    user: RoleClaim
    buyer: RoleClaim
    decision_maker: RoleClaim
    economic_beneficiary: RoleClaim
    budget_owner: RoleClaim | None = Field(
        description="whose budget pays; a hypothesis when no claim states it; null only when "
        "no one in the segment plausibly pays"
    )


class Channel(BaseModel):
    kind: ChannelKind
    name: str
    claim_ids: list[int] = Field(description="numbers of table claims that name it, or []")


class OpportunityDraft(BaseModel):
    segment: str = Field(description="English, specific: 'Road freight firms with 5–50 trucks'")
    solution_angle: str = Field(description="English, one sentence: what the B2B SaaS does")
    gap_claim_ids: list[int] = Field(description="numbers of the gap claims it targets")
    buyer_roles: BuyerRoles
    channels: list[Channel] = Field(description="1–4 places to reach these buyers")
    breadth_hint: str | None = Field(
        description="what official statistic would count these companies, or null"
    )


class BuyerAnalysis(BaseModel):
    """Output of ``buyers``: 1–3 opportunities for one problem."""

    opportunities: list[OpportunityDraft]
