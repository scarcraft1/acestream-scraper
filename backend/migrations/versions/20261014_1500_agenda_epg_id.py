"""Let a TV channel take football fixtures from an agenda guide channel.

The revision id is 20261014_1500 on purpose, although it runs after
20261021_1200: it is the id the fork's last migration had before it was rebuilt
on upstream develop (that chain was 1200 stream_url, 1300 stream_headers,
1400 agenda_epg_id, 1500 a data fix). A database already at that revision has
all three columns, so it needs no upgrade; a new one gets develop's columns
first and then this one.
"""
from alembic import op
import sqlalchemy as sa

revision = '20261014_1500'
down_revision = '20261021_1200'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tv_channels',
                  sa.Column('agenda_epg_id', sa.String(255), nullable=True))


def downgrade():
    with op.batch_alter_table('tv_channels') as batch:
        batch.drop_column('agenda_epg_id')
