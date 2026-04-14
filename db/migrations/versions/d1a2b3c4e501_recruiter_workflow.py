"""recruiter_workflow

Phase 3: candidate status tracking + search templates

Revision ID: d1a2b3c4e501
Revises: c9e1f3a2b701
Create Date: 2026-04-14

"""
from alembic import op
import sqlalchemy as sa

revision = 'd1a2b3c4e501'
down_revision = 'c9e1f3a2b701'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Candidate: recruiter workflow columns ──────────────────────────────
    op.add_column('candidates',
        sa.Column('recruiter_status', sa.String(32), nullable=False, server_default='new'))
    op.add_column('candidates',
        sa.Column('notes', sa.Text(), nullable=True))
    op.add_column('candidates',
        sa.Column('status_updated_at', sa.DateTime(timezone=True), nullable=True))

    # ── search_templates table ─────────────────────────────────────────────
    op.create_table(
        'search_templates',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True),
        sa.Column('name', sa.String(256), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('template_jd', sa.Text(), nullable=False),
        sa.Column('location_filter', sa.String(128), nullable=True),
        sa.Column('max_candidates', sa.Integer(), nullable=False,
                  server_default='25'),
        sa.Column('use_count', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('is_deleted', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('search_templates')
    op.drop_column('candidates', 'status_updated_at')
    op.drop_column('candidates', 'notes')
    op.drop_column('candidates', 'recruiter_status')
