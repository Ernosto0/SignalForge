"""M5 commercial: opportunity status, knockouts, accessibility and market breadth

Revision ID: f1a7c3d9e2b4
Revises: e6f1a3b5c7d9
Create Date: 2026-10-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f1a7c3d9e2b4"
down_revision: str | Sequence[str] | None = "e6f1a3b5c7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("opportunities", sa.Column("status", sa.String(length=16), nullable=True))
    for name, default in (
        ("knockouts", "[]"),
        ("accessibility", "{}"),
        ("market_breadth", "{}"),
    ):
        op.add_column(
            "opportunities",
            sa.Column(
                name,
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=default,
                nullable=False,
            ),
        )


def downgrade() -> None:
    """Downgrade schema."""
    for name in ("market_breadth", "accessibility", "knockouts", "status"):
        op.drop_column("opportunities", name)
