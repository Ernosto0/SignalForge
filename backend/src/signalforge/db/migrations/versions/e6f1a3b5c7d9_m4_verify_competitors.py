"""M4 verify & competitors: entailment, claim meta, document origin, excerpt stage, verification

Revision ID: e6f1a3b5c7d9
Revises: d5e9a2b3c4f6
Create Date: 2026-10-06 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e6f1a3b5c7d9"
down_revision: str | Sequence[str] | None = "d5e9a2b3c4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("claims", sa.Column("entailment", sa.String(length=16), nullable=True))
    op.add_column(
        "claims",
        sa.Column(
            "meta", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
    )
    op.create_index("ix_claims_run_id_kind", "claims", ["run_id", "kind"], unique=False)

    op.add_column(
        "documents",
        sa.Column("origin", sa.String(length=16), server_default="collect", nullable=False),
    )
    op.add_column("documents", sa.Column("problem_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_documents_problem_id_problem_clusters"),
        "documents",
        "problem_clusters",
        ["problem_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "excerpts",
        sa.Column("stage", sa.String(length=32), server_default="extract", nullable=False),
    )

    op.add_column(
        "problem_clusters",
        sa.Column("verification", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # Gap matrices had no run id; existing rows (none expected before M4) are dropped.
    op.execute("DELETE FROM gap_matrices")
    op.add_column("gap_matrices", sa.Column("run_id", sa.BigInteger(), nullable=False))
    op.create_foreign_key(
        op.f("fk_gap_matrices_run_id_research_runs"),
        "gap_matrices",
        "research_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_gap_matrices_run_id"), "gap_matrices", ["run_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_gap_matrices_run_id"), table_name="gap_matrices")
    op.drop_constraint(
        op.f("fk_gap_matrices_run_id_research_runs"), "gap_matrices", type_="foreignkey"
    )
    op.drop_column("gap_matrices", "run_id")
    op.drop_column("problem_clusters", "verification")
    op.drop_column("excerpts", "stage")
    op.drop_constraint(
        op.f("fk_documents_problem_id_problem_clusters"), "documents", type_="foreignkey"
    )
    op.drop_column("documents", "problem_id")
    op.drop_column("documents", "origin")
    op.drop_index("ix_claims_run_id_kind", table_name="claims")
    op.drop_column("claims", "meta")
    op.drop_column("claims", "entailment")
