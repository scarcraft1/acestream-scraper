"""Device playlists: per-device choice of channel groups and channels."""
from alembic import op
import sqlalchemy as sa

revision = '20261113_1200'
down_revision = '20261112_1200'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('device_playlists',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=True),
        sa.Column('disabled_groups', sa.JSON(), nullable=False),
        sa.Column('enabled_channels', sa.JSON(), nullable=False),
        sa.Column('disabled_channels', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_device_playlists_owner_id', 'device_playlists', ['owner_id'])


def downgrade():
    op.drop_index('ix_device_playlists_owner_id', table_name='device_playlists')
    op.drop_table('device_playlists')
