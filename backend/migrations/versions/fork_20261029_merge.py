"""Join the fork's head (fork_20261027_merge) to develop's head of 2026-10-29.

The box and the bench are at fork_20261027_merge. Joined as a merge rather
than re-chained, a database at that revision runs only develop's missing
revisions (recordings and anything before it in develop's chain) and a fresh
one runs everything.
"""

revision = 'fork_20261029_merge'
down_revision = ('fork_20261027_merge', '20261028_1200')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
