"""phase2 ai engine columns

Revision ID: a3f9c1e2d847
Revises: 1cb5ba355d7f
Create Date: 2026-03-24

Adds columns introduced by the Phase 2 AI engine upgrade:
  searches:   intelligence_report (JSON)
  candidates: skill_match_detail (JSON), skill_gaps (JSON)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

# revision identifiers
revision = 'a3f9c1e2d847'
down_revision = '1cb5ba355d7f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('searches',
        sa.Column('intelligence_report', JSON, nullable=True)
    )
    op.add_column('candidates',
        sa.Column('skill_match_detail', JSON, nullable=True)
    )
    op.add_column('candidates',
        sa.Column('skill_gaps', JSON, nullable=True)
    )


def downgrade() -> None:
    op.drop_column('candidates', 'skill_gaps')
    op.drop_column('candidates', 'skill_match_detail')
    op.drop_column('searches', 'intelligence_report')
