"""Streams without a TV channel: dashboard count and webhook report (synthetic names only)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, inspect

from tests.migration_test_utils import database_url_for, downgrade_to_revision, upgrade_to_revision

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def _stream(db_session, index, added_at=None, tv_channel_id=None, active=True):
    from app.models.models import AcestreamChannel
    row = AcestreamChannel(id=f'{index:040x}', name=f'Example stream {index}', source_url='https://example.test/list.m3u',
                           is_active=active, tv_channel_id=tv_channel_id, added_at=added_at)
    db_session.add(row)
    db_session.commit()
    return row


def test_summary_counts_loose_streams_and_the_recent_ones(db_session):
    from app.models.models import TVChannel
    from app.services import unassigned_streams
    tv = TVChannel(name='Example channel', is_active=True)
    db_session.add(tv)
    db_session.commit()
    legacy = _stream(db_session, 1)
    legacy.added_at = None                                         # older than the column
    db_session.commit()
    _stream(db_session, 2, added_at=NOW - timedelta(days=30))
    _stream(db_session, 3, added_at=NOW - timedelta(days=1))
    _stream(db_session, 4, added_at=NOW - timedelta(hours=1), tv_channel_id=tv.id)
    _stream(db_session, 5, added_at=NOW - timedelta(hours=1), active=False)
    assert unassigned_streams.summary(db_session, now=NOW) == {
        'total': 3, 'recent': 1, 'recent_days': 7, 'examples': ['Example stream 3']}


def test_the_report_counts_only_what_arrived_since_the_last_one(db_session):
    from app.services import unassigned_streams
    _stream(db_session, 1, added_at=NOW - timedelta(days=1))
    assert unassigned_streams.new_since_notice(db_session, now=NOW) is None   # first call only sets the mark
    _stream(db_session, 2, added_at=NOW + timedelta(minutes=5))
    _stream(db_session, 3, added_at=NOW + timedelta(minutes=6))
    later = NOW + timedelta(hours=1)
    assert unassigned_streams.new_since_notice(db_session, now=later) == {
        'new': 2, 'total': 3, 'examples': ['Example stream 3', 'Example stream 2']}
    assert unassigned_streams.new_since_notice(db_session, now=later + timedelta(hours=1)) is None


def test_the_webhook_message_names_the_streams_and_nothing_else(monkeypatch):
    from app.services import notification_service
    sent = []
    monkeypatch.setattr(notification_service, 'notify', lambda *args: sent.append(args) or True)
    assert notification_service.unassigned_streams(None) is False
    assert notification_service.unassigned_streams({'new': 2, 'total': 9, 'examples': ['Example A', 'Example B']})
    assert sent == [('streams.unassigned', '2 new source(s) without a TV channel', '9 in total. Recent: Example A, Example B',
                     {'new': 2, 'total': 9, 'examples': ['Example A', 'Example B']})]


def test_the_dashboard_endpoint(client, db_session):
    _stream(db_session, 1, added_at=datetime.now(timezone.utc))
    body = client.get('/api/v1/acestream-channels/unassigned_summary').json()
    assert body == {'total': 1, 'recent': 1, 'recent_days': 7, 'examples': ['Example stream 1']}


def test_a_new_stream_gets_its_arrival_time(db_session):
    from app.repositories.channel_repository import ChannelRepository
    before = datetime.now(timezone.utc)
    row = ChannelRepository(db_session).create_or_update_channel(channel_id='a' * 40, name='Example new',
                                                                  source_url='https://example.test/list.m3u')
    assert row.added_at is not None and row.added_at >= before


def test_stream_added_at_migration_upgrade_and_downgrade(tmp_path):
    path = tmp_path / 'added-at.sqlite'
    upgrade_to_revision(path, '20261113_1300')
    engine = create_engine(database_url_for(path))
    try:
        assert 'added_at' in {c['name'] for c in inspect(engine).get_columns('acestream_channels')}
        downgrade_to_revision(path, '20261113_1200')
        assert 'added_at' not in {c['name'] for c in inspect(engine).get_columns('acestream_channels')}
    finally:
        engine.dispose()
