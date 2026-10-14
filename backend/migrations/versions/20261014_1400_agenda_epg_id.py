"""Let a TV channel take football fixtures from an agenda guide channel."""
from alembic import op
import sqlalchemy as sa

revision = '20261014_1400'
down_revision = '20261014_1300'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tv_channels',
                  sa.Column('agenda_epg_id', sa.String(255), nullable=True))


def downgrade():
    with op.batch_alter_table('tv_channels') as batch:
        batch.drop_column('agenda_epg_id')
