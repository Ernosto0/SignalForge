"""problem discovery: signal meta, cluster inference claim

Revision ID: d5e9a2b3c4f6
Revises: c4d8e1f2a7b9
Create Date: 2026-10-05 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d5e9a2b3c4f6"
down_revision: str | Sequence[str] | None = "c4d8e1f2a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "signals",
        sa.Column(
            "meta", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
    )
    op.add_column("problem_clusters", sa.Column("claim_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_problem_clusters_claim_id_claims"),
        "problem_clusters",
        "claims",
        ["claim_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_claims_run_id_stage", "claims", ["run_id", "stage"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_claims_run_id_stage", table_name="claims")
    op.drop_constraint(
        op.f("fk_problem_clusters_claim_id_claims"), "problem_clusters", type_="foreignkey"
    )
    op.drop_column("problem_clusters", "claim_id")
    op.drop_column("signals", "meta")
