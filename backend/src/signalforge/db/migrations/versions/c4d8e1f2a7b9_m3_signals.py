"""m3 signals: excerpt run/source, cluster strength and Gate 1 fields

Revision ID: c4d8e1f2a7b9
Revises: b7e2f9a1c3d8
Create Date: 2026-10-05 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4d8e1f2a7b9"
down_revision: str | Sequence[str] | None = "b7e2f9a1c3d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Excerpts were only reachable through documents; stages replace a run's excerpts directly.
    # Nothing wrote excerpts before M3, so the table is empty and the column can be NOT NULL.
    op.add_column("excerpts", sa.Column("run_id", sa.BigInteger(), nullable=False))
    op.create_foreign_key(
        op.f("fk_excerpts_run_id_research_runs"),
        "excerpts",
        "research_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_excerpts_run_id"), "excerpts", ["run_id"], unique=False)
    op.add_column(
        "excerpts",
        sa.Column("source", sa.String(length=8), server_default="text", nullable=False),
    )

    op.add_column(
        "problem_clusters",
        sa.Column(
            "signal_type_mix",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "problem_clusters",
        sa.Column(
            "strength", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
    )
    op.add_column(
        "problem_clusters",
        sa.Column("shortlisted", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "problem_clusters",
        sa.Column(
            "gate_trace",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column("problem_clusters", sa.Column("rank", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for column in ("rank", "gate_trace", "shortlisted", "strength", "signal_type_mix"):
        op.drop_column("problem_clusters", column)
    op.drop_column("excerpts", "source")
    op.drop_index(op.f("ix_excerpts_run_id"), table_name="excerpts")
    op.drop_constraint(op.f("fk_excerpts_run_id_research_runs"), "excerpts", type_="foreignkey")
    op.drop_column("excerpts", "run_id")
