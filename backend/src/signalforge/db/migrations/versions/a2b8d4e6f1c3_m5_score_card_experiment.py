"""M5 scoring: score card experiment; score fields nullable for knocked-out cards

Revision ID: a2b8d4e6f1c3
Revises: f1a7c3d9e2b4
Create Date: 2026-10-10 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a2b8d4e6f1c3"
down_revision: str | Sequence[str] | None = "f1a7c3d9e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Null on a knocked-out opportunity's card: not assessed.
ASSESSED = (
    ("attractiveness", sa.Float()),
    ("confidence", sa.String(length=8)),
    ("founder_fit", sa.String(length=8)),
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "score_cards",
        sa.Column("experiment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    for name, type_ in ASSESSED:
        op.alter_column("score_cards", name, existing_type=type_, nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM score_cards WHERE attractiveness IS NULL")
    for name, type_ in ASSESSED:
        op.alter_column("score_cards", name, existing_type=type_, nullable=False)
    op.drop_column("score_cards", "experiment")
