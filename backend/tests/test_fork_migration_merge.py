"""The fork's agenda migration is a branch joined to develop's head."""
from sqlalchemy import create_engine, inspect, text
from migration_test_utils import database_url_for, upgrade_to_revision


def _columns(engine, table):
    return {column['name'] for column in inspect(engine).get_columns(table)}


def test_a_database_at_the_forks_old_head_gets_develops_later_migrations(tmp_path):
    # The box before this rebase: develop up to 20261021_1200 plus the agenda column.
    path = tmp_path / 'fork-head.sqlite'
    upgrade_to_revision(path, '20261014_1500')
    engine = create_engine(database_url_for(path))
    try:
        assert 'agenda_epg_id' in _columns(engine, 'tv_channels')
        assert 'stream_order' not in _columns(engine, 'acestream_channels')
        upgrade_to_revision(path, 'head')
        assert 'stream_order' in _columns(engine, 'acestream_channels')
        assert 'agenda_epg_id' in _columns(engine, 'tv_channels')
        with engine.connect() as db:
            assert db.execute(text('SELECT version_num FROM alembic_version')).scalar() == 'fork_20261029_merge'
    finally:
        engine.dispose()


def test_a_database_at_the_27_10_merge_gets_develops_recordings(tmp_path):
    # The box and the bench on 2026-10-29: fork_20261027_merge, before develop's recordings.
    path = tmp_path / 'fork-2710.sqlite'
    upgrade_to_revision(path, 'fork_20261027_merge')
    engine = create_engine(database_url_for(path))
    try:
        assert 'recordings' not in inspect(engine).get_table_names()
        upgrade_to_revision(path, 'head')
        assert 'recordings' in inspect(engine).get_table_names()
        assert 'agenda_epg_id' in _columns(engine, 'tv_channels')
        with engine.connect() as db:
            assert db.execute(text('SELECT version_num FROM alembic_version')).scalar() == 'fork_20261029_merge'
    finally:
        engine.dispose()
