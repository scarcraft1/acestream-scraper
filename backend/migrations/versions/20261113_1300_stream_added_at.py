"""Streams remember when they entered the catalogue, to report new ones without a TV channel."""
from alembic import op
import sqlalchemy as sa

revision = '20261113_1300'
down_revision = '20261113_1200'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('acestream_channels') as batch:
        batch.add_column(sa.Column('added_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_acestream_channels_added_at', 'acestream_channels', ['added_at'])


def downgrade():
    op.drop_index('ix_acestream_channels_added_at', table_name='acestream_channels')
    with op.batch_alter_table('acestream_channels') as batch:
        batch.drop_column('added_at')
