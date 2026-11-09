"""Merge the fork's box revision with develop's offline retention.

20261104_1200 is the fork's empty revision that the box already records, and
develop's 20261107_1200 (offline streaks) also follows 20261102_1200. Neither
may change id or parent, so this empty revision joins both heads: the box goes
20261104_1200 -> 20261107_1200 -> here, a develop database the other way round.
"""

revision = '20261109_1200'
down_revision = ('20261104_1200', '20261107_1200')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
