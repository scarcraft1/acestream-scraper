"""Merge the box's 20261109_1200 with develop's EPG channel identities.

Develop's EPG identities migration was published as 20261109_1200, an id the
fork's merge revision had already taken and the box already records. A
migration that ran on the box keeps its id (FORK.md §5), so in the fork
develop's migration is 20261109_1201, with its own parent 20261107_1200, and
this empty revision joins both heads. A box at 20261109_1200 runs the EPG
identities migration and then this one.

When develop adds a migration whose parent is its 20261109_1200, the fork
points it at 20261110_1200 instead.
"""

revision = '20261110_1200'
down_revision = ('20261109_1200', '20261109_1201')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
