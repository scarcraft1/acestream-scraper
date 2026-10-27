"""Join the fork's agenda column (20261014_1500) to develop's later chain.

20261014_1500 hangs off 20261021_1200 and keeps that id because a database
already at it (the fork before this rebase) must not run it again. Chaining it
after develop's head instead would make alembic believe such a database has
develop's newer migrations too (W3U catalogues, sporting events, stream
order) and skip them. As a branch joined here, a database at 20261014_1500
runs only develop's missing revisions and a fresh one runs everything.
"""

revision = 'fork_20261027_merge'
down_revision = ('20261026_1200', '20261014_1500')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
