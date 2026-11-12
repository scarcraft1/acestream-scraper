"""Channel rules tree runs: what each application changed, so it can be undone."""
from alembic import op
import sqlalchemy as sa

revision = '20261112_1200'
down_revision = '20261111_1200'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('channel_tree_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('trigger', sa.String(length=16), nullable=False),
        sa.Column('stats', sa.JSON(), nullable=False),
        sa.Column('undone_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_channel_tree_runs_created_at', 'channel_tree_runs', ['created_at'])
    op.create_table('channel_tree_changes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('channel_tree_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('stream_id', sa.String(length=255), nullable=True),
        sa.Column('tv_channel_id', sa.Integer(), nullable=True),
        sa.Column('before', sa.JSON(), nullable=True),
        sa.Column('after', sa.JSON(), nullable=True))
    op.create_index('ix_channel_tree_changes_run_id', 'channel_tree_changes', ['run_id'])


def downgrade():
    op.drop_index('ix_channel_tree_changes_run_id', table_name='channel_tree_changes')
    op.drop_table('channel_tree_changes')
    op.drop_index('ix_channel_tree_runs_created_at', table_name='channel_tree_runs')
    op.drop_table('channel_tree_runs')
