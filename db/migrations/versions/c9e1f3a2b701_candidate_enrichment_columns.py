"""candidate enrichment columns

Revision ID: c9e1f3a2b701
Revises: b7d2e4f1c093
Create Date: 2026-04-08

Adds 6 enrichment columns to candidates that were previously computed
on-the-fly but never persisted — causing schema drift between the Pydantic
models and the ORM. Now persisted in _finalize_node.

  candidates:
    skills              JSON      list[str] from Apollo enrichment
    employment_history  JSON      list[dict] from Apollo
    avg_tenure_months   FLOAT     computed average tenure
    is_job_hopper       BOOLEAN   True if avg_tenure < 18 months
    career_trajectory   VARCHAR   ascending | flat | descending
    email_validity      VARCHAR   verified | unverified | risky | missing
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision = 'c9e1f3a2b701'
down_revision = 'b7d2e4f1c093'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('candidates', sa.Column('skills', JSON, nullable=True))
    op.add_column('candidates', sa.Column('employment_history', JSON, nullable=True))
    op.add_column('candidates', sa.Column('avg_tenure_months', sa.Float, nullable=True))
    op.add_column('candidates', sa.Column('is_job_hopper', sa.Boolean, nullable=True))
    op.add_column('candidates', sa.Column('career_trajectory', sa.String(32), nullable=True))
    op.add_column('candidates', sa.Column('email_validity', sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column('candidates', 'email_validity')
    op.drop_column('candidates', 'career_trajectory')
    op.drop_column('candidates', 'is_job_hopper')
    op.drop_column('candidates', 'avg_tenure_months')
    op.drop_column('candidates', 'employment_history')
    op.drop_column('candidates', 'skills')
