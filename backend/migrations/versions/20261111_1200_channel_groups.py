"""Channel groups: Kodi tabs in tree order, with a stable block of channel numbers per top-level group."""
from alembic import op
import sqlalchemy as sa

revision = '20261111_1200'
down_revision = '20261109_1300'  # fork: follows develop's channel categories, the box's head since 0f7b030d
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('channel_groups',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('parent_id', sa.Integer(), sa.ForeignKey('channel_groups.id', ondelete='SET NULL'), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('number_block', sa.Integer(), nullable=True),
        sa.Column('member_order', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('name', name='uq_channel_groups_name'))
    op.create_index('ix_channel_groups_parent_id', 'channel_groups', ['parent_id'])


def downgrade():
    op.drop_index('ix_channel_groups_parent_id', table_name='channel_groups')
    op.drop_table('channel_groups')
