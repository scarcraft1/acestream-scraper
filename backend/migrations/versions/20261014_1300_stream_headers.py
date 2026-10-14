"""Keep the HTTP headers a playlist publishes for an IPTV stream."""
from alembic import op
import sqlalchemy as sa

revision = '20261014_1300'
down_revision = '20261014_1200'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('acestream_channels',
                  sa.Column('stream_headers', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('acestream_channels') as batch:
        batch.drop_column('stream_headers')
