"""search_quality_fields

Phase 4: persist search_quality_score + recommended_jd_changes on the Search row
so clients polling GET /api/search/{id}/status see quality data without a second request.

Revision ID: e2b3c4d5f601
Revises: d1a2b3c4e501
Create Date: 2026-04-15

"""
from alembic import op
import sqlalchemy as sa

revision = 'e2b3c4d5f601'
down_revision = 'd1a2b3c4e501'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('searches',
        sa.Column('search_quality_score', sa.Integer(), nullable=True))
    op.add_column('searches',
        sa.Column('recommended_jd_changes', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('searches', 'recommended_jd_changes')
    op.drop_column('searches', 'search_quality_score')
