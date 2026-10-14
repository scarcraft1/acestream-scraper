"""Store a stream without headers as SQL NULL, not the JSON text 'null'."""
from alembic import op

revision = '20261014_1500'
down_revision = '20261014_1400'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE acestream_channels SET stream_headers = NULL "
               "WHERE stream_headers = 'null'")


def downgrade():
    # Nothing to undo: NULL and 'null' read back the same.
    pass
