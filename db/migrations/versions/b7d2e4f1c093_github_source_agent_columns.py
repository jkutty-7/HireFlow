"""github source agent columns

Revision ID: b7d2e4f1c093
Revises: a3f9c1e2d847
Create Date: 2026-03-25

Adds columns for the GitHub Repo Source Agent:
  candidates: source (VARCHAR), source_repos (JSON)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

# revision identifiers
revision = 'b7d2e4f1c093'
down_revision = 'a3f9c1e2d847'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('candidates',
        sa.Column('source', sa.String(32), nullable=False, server_default='apollo')
    )
    op.add_column('candidates',
        sa.Column('source_repos', JSON, nullable=True)
    )


def downgrade() -> None:
    op.drop_column('candidates', 'source_repos')
    op.drop_column('candidates', 'source')
