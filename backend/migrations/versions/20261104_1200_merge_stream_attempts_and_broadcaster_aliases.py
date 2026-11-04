"""Empty revision after the playback history (20261102_1200).

It joined the history and develop's event broadcaster aliases (20261103_1200)
while both followed 20261030_1300: the fork's box applied the history first.
Since #308 develop has the history after 20261103_1200, so a single parent is
enough. The box's alembic_version says 20261104_1200, so the revision stays:
code that does not know it would not start there. Rehearsed on a copy of the
box database, a develop one at 20261102_1200 and an empty one (05-11).
"""

revision = '20261104_1200'
down_revision = '20261102_1200'
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
