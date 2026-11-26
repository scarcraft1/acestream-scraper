"""The fork's migration history and the box that has already run it.

The box records every revision below in its database. Alembic finds what to
run by walking parents from the box's head, so a revision the box has run must
keep its id and its parents, or an upgrade either skips a migration or runs one
again over tables that exist (FORK.md §5). A new develop migration that makes
a second head gets a merge revision of the fork's own; develop's files are not
edited.
"""
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from tests.migration_test_utils import (
    REPO_ROOT,
    current_head_revision,
    database_url_for,
    downgrade_to_revision,
    upgrade_to_head,
    upgrade_to_revision,
)

# Revision -> parents, as the box ran them (fork e224c8c5, 2026-11-18). Grows
# only when a deploy runs a new revision on the box; nothing here changes.
BOX_HISTORY = {
    '20250330_add_added_at': ('YtQg2uH3sL7m',),
    '20250406_add_epg_tables_and_epg_update_protected_field': ('20250330_add_added_at',),
    '20250409_add_missing_fields_to_epg_sources_tables': ('20250406_add_epg_tables_and_epg_update_protected_field',),
    '20250409_add_tv_channels': ('20250409_add_missing_fields_to_epg_sources_tables',),
    '20250412_add_epg_channels_update_tv_channels': ('20250409_add_tv_channels',),
    '20260423_1105': ('phase6_add_hotpath_indexes',),
    '20260423_1345': ('20260423_1105',),
    '20260504_1200': ('20260423_1345',),
    '20260824_1000': ('20260504_1200',),
    '20260824_1200': ('20260824_1000',),
    '20260903_1200': ('20260824_1200',),
    '20260906_1300': ('20260903_1200',),
    '20260907_1500': ('20260906_1300',),
    '20260914_1200': ('20260907_1500',),
    '20260915_1200': ('20260914_1200',),
    '20260928_1200': ('20260915_1200',),
    '20261021_1200': ('20260928_1200',),
    '20261022_1200': ('20261021_1200',),
    '20261023_1200': ('20261021_1200',),
    '20261023_1300': ('20261023_1200',),
    '20261023_1400': ('20261022_1200', '20261023_1300'),
    '20261023_1500': ('20261023_1400',),
    '20261026_1200': ('20261023_1500',),
    '20261027_1200': ('20261026_1200',),
    '20261027_1300': ('20261027_1200',),
    '20261028_1200': ('20261027_1300',),
    '20261030_1200': ('20261028_1200',),
    '20261030_1300': ('20261030_1200',),
    '20261102_1200': ('20261103_1200',),
    '20261103_1200': ('20261030_1300',),
    '20261104_1200': ('20261102_1200',),
    '20261107_1200': ('20261102_1200',),
    '20261109_1200': ('20261104_1200', '20261107_1200'),
    '20261109_1201': ('20261107_1200',),
    '20261109_1300': ('20261110_1200',),
    '20261110_1200': ('20261109_1200', '20261109_1201'),
    '20261111_1200': ('20261109_1300',),
    '20261112_1200': ('20261111_1200',),
    '20261113_1200': ('20261112_1200',),
    '20261113_1300': ('20261113_1200',),
    '20261113_1400': ('20261113_1300',),
    '4e3b1a9c8f21': ('channel_status_fields_02',),
    'KbBg5DH3tkxp': ('t36GeaTCrBqm',),
    'YtQg2uH3sL7m': ('ZgB93dF5Rt7p',),
    'ZgB93dF5Rt7p': ('KbBg5DH3tkxp',),
    'add_activity_log_table': ('20250412_add_epg_channels_update_tv_channels',),
    'add_channel_id_to_activity_log': ('add_activity_log_table',),
    'channel_status_fields_02': ('fb06651a94c1',),
    'd626bf79a25c': ('4e3b1a9c8f21',),
    'fb06651a94c1': (),
    'h6s6bgh82cxa': ('d626bf79a25c',),
    'phase6_add_hotpath_indexes': ('add_channel_id_to_activity_log',),
    't36GeaTCrBqm': ('h6s6bgh82cxa',),
}


def _scripts():
    config = Config(str(REPO_ROOT / 'backend/migrations/alembic.ini'))
    config.set_main_option('script_location', str(REPO_ROOT / 'backend/migrations'))
    return ScriptDirectory.from_config(config)


def test_the_history_has_a_single_head():
    assert len(_scripts().get_heads()) == 1


def _parents(down_revision):
    if down_revision is None:
        return ()
    if isinstance(down_revision, str):
        return (down_revision,)
    return tuple(sorted(down_revision))


def test_every_revision_the_box_ran_keeps_its_id_and_parents():
    history = {script.revision: _parents(script.down_revision) for script in _scripts().walk_revisions()}
    ran = {revision: history.get(revision, 'missing') for revision in BOX_HISTORY}
    assert ran == {revision: _parents(parents) for revision, parents in BOX_HISTORY.items()}


def test_a_database_at_the_box_merge_revision_still_gets_the_epg_identities(tmp_path):
    """Develop published its EPG identities migration as 20261109_1200, the id
    the box records for the fork's merge revision. In the fork that migration
    is 20261109_1201, so a database at the box's 20261109_1200 must still
    create its table on the way to the head."""
    import sqlite3
    from contextlib import closing
    path = tmp_path / 'box.db'
    upgrade_to_revision(path, '20261109_1200')
    engine = create_engine(database_url_for(path))
    assert 'epg_channel_identities' not in inspect(engine).get_table_names()
    engine.dispose()
    upgrade_to_head(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0] == current_head_revision()
    engine = create_engine(database_url_for(path))
    assert 'epg_channel_identities' in inspect(engine).get_table_names()
    engine.dispose()


def test_box_revision_follows_the_history_and_downgrades_to_it(tmp_path):
    """20261104_1200 is the fork's empty revision the box already records: a
    database there reaches the merge head with develop's health columns (and
    without offline_since, which develop drops), and going back leaves the
    history table in place."""
    import sqlite3
    from contextlib import closing
    path = tmp_path / 'box.db'
    upgrade_to_revision(path, '20261104_1200')
    upgrade_to_head(path)

    def version():
        with closing(sqlite3.connect(path)) as db:
            return db.execute('SELECT version_num FROM alembic_version').fetchone()[0]

    assert version() == current_head_revision()
    engine = create_engine(database_url_for(path))
    columns = {c['name'] for c in inspect(engine).get_columns('acestream_channels')}
    assert {'last_worked_at', 'failing_since'} <= columns and 'offline_since' not in columns
    engine.dispose()
    downgrade_to_revision(path, '20261102_1200')
    assert version() == '20261102_1200'
    engine = create_engine(database_url_for(path))
    assert 'stream_attempts' in inspect(engine).get_table_names()
    engine.dispose()
