"""url candidates

Revision ID: b7e2f9a1c3d8
Revises: a3c1d2e4f5b6
Create Date: 2026-10-05 18:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7e2f9a1c3d8"
down_revision: str | Sequence[str] | None = "a3c1d2e4f5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "url_candidates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("source_category", sa.String(length=32), nullable=True),
        sa.Column("quality_tier", sa.String(length=8), nullable=False),
        sa.Column("access", sa.String(length=16), nullable=False),
        sa.Column("query_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("best_rank", sa.Integer(), nullable=False),
        sa.Column("off_site", sa.Boolean(), nullable=False),
        sa.Column("decision", sa.String(length=8), nullable=False),
        sa.Column("decision_rule", sa.String(length=24), nullable=False),
        sa.Column("triage_score", sa.Integer(), nullable=True),
        sa.Column("triage_label", sa.String(length=24), nullable=True),
        sa.Column("triage_reason", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("fetch_status", sa.String(length=24), nullable=True),
        sa.Column("fetch_error", sa.Text(), nullable=True),
        sa.Column("document_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_url_candidates_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_runs.id"],
            name=op.f("fk_url_candidates_run_id_research_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_url_candidates")),
        sa.UniqueConstraint("run_id", "canonical_url", name=op.f("uq_url_candidates_run_id")),
    )
    op.create_index(op.f("ix_url_candidates_run_id"), "url_candidates", ["run_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_url_candidates_run_id"), table_name="url_candidates")
    op.drop_table("url_candidates")
