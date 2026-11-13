"""Device playlists: per-device groups and channels (synthetic names only)."""

import uuid

import pytest
from sqlalchemy import create_engine, inspect

from tests.migration_test_utils import database_url_for, downgrade_to_revision, upgrade_to_revision

RELAY = 'http://scraper.test/tuner/channel/{tv_channel_id}.ts'


@pytest.fixture
def catalogue(db_session):
    """Groups Alpha, Beta, Gamma > Gamma Sub; one stream per TV channel."""
    from app.models.models import AcestreamChannel, ChannelGroup, TVChannel

    alpha = ChannelGroup(name='Alpha', parent_id=None, position=0, member_order=[])
    beta = ChannelGroup(name='Beta', parent_id=None, position=1, member_order=[])
    gamma = ChannelGroup(name='Gamma', parent_id=None, position=2, member_order=[])
    db_session.add_all([alpha, beta, gamma])
    db_session.flush()
    sub = ChannelGroup(name='Gamma Sub', parent_id=gamma.id, position=0, member_order=[])
    db_session.add(sub)
    db_session.flush()
    tvs = {}
    for name, category in [('One', 'Alpha'), ('Two', 'Beta; Alpha'), ('Three', 'Gamma Sub'), ('Four', 'Gamma')]:
        tv = TVChannel(name=f'Example {name}', category=category, is_active=True)
        db_session.add(tv)
        db_session.flush()
        db_session.add(AcestreamChannel(id=uuid.uuid4().hex[:40].ljust(40, '0'), name=f'Stream {name}',
                                        source_url='https://example.test/list.m3u', is_active=True,
                                        is_online=True, tv_channel_id=tv.id))
        tvs[name] = tv
    db_session.commit()
    return dict(groups=dict(alpha=alpha, beta=beta, gamma=gamma, sub=sub), tvs=tvs)


def _playlist(db_session, **fields):
    from app.models.models import DevicePlaylist
    row = DevicePlaylist(**{'name': 'Example device', 'disabled_groups': [], 'enabled_channels': [],
                            'disabled_channels': [], **fields})
    db_session.add(row)
    db_session.commit()
    return row


def test_a_disabled_group_leaves_out_its_subgroups(db_session, catalogue):
    from app.services.device_playlist_service import build_filter
    g, t = catalogue['groups'], catalogue['tvs']
    filt = build_filter(db_session, _playlist(db_session, disabled_groups=[g['gamma'].id]))
    assert filt.hidden == {t['Three'].id, t['Four'].id}


def test_any_disabled_group_hides_unless_the_channel_is_enabled_on_its_own(db_session, catalogue):
    from app.services.device_playlist_service import build_filter
    g, t = catalogue['groups'], catalogue['tvs']
    only_alpha = [g['beta'].id, g['gamma'].id]
    filt = build_filter(db_session, _playlist(db_session, disabled_groups=only_alpha))
    assert filt.hidden == {t['Two'].id, t['Three'].id, t['Four'].id}

    filt = build_filter(db_session, _playlist(db_session, disabled_groups=only_alpha, enabled_channels=[t['Two'].id]))
    assert t['Two'].id not in filt.hidden
    # It keeps only its enabled tab, with the stored separator.
    assert filt.title(t['Two']) == 'Alpha'


def test_a_channel_enabled_with_every_group_disabled_keeps_its_own_tabs(db_session, catalogue):
    from app.services.device_playlist_service import build_filter
    g, t = catalogue['groups'], catalogue['tvs']
    every = [x.id for x in g.values()]
    filt = build_filter(db_session, _playlist(db_session, disabled_groups=every, enabled_channels=[t['Two'].id]))
    assert filt.hidden == {t['One'].id, t['Three'].id, t['Four'].id}
    assert filt.title(t['Two']) == 'Beta; Alpha'


def test_a_channel_disabled_on_its_own_goes_whatever_its_groups(db_session, catalogue):
    from app.services.device_playlist_service import build_filter
    t = catalogue['tvs']
    filt = build_filter(db_session, _playlist(db_session, disabled_channels=[t['One'].id]))
    assert filt.hidden == {t['One'].id}


def test_channels_without_a_group_are_one_more_group(client, db_session, catalogue):
    from app.models.models import TVChannel
    from app.services.device_playlist_service import UNGROUPED, build_filter
    loose = TVChannel(name='Example Loose', category=None, is_active=True)
    db_session.add(loose)
    db_session.commit()
    assert loose.id not in build_filter(db_session, _playlist(db_session)).hidden
    filt = build_filter(db_session, _playlist(db_session, disabled_groups=[UNGROUPED]))
    assert filt.hidden == {loose.id}
    filt = build_filter(db_session, _playlist(db_session, disabled_groups=[UNGROUPED], enabled_channels=[loose.id]))
    assert filt.hidden == set()

    groups = client.get('/api/v1/device-playlists/groups').json()
    assert groups[-1] == {'id': UNGROUPED, 'name': 'No group', 'parent_id': None, 'position': 3, 'channels': 1}
    created = client.post('/api/v1/device-playlists', json={'name': 'Example kids'}).json()
    body = client.put(f"/api/v1/device-playlists/{created['id']}", json={'disabled_groups': [UNGROUPED]}).json()
    assert body['disabled_groups'] == [UNGROUPED] and body['channels'] == 4


def test_the_m3u_applies_the_list_and_stays_whole_without_it(client, db_session, catalogue):
    g, t = catalogue['groups'], catalogue['tvs']
    row = _playlist(db_session, disabled_groups=[g['beta'].id, g['gamma'].id], enabled_channels=[t['Two'].id])
    params = {'base_url': RELAY, 'only_online': 'false'}
    whole = client.get('/api/v1/playlists/m3u', params=params)
    listed = client.get('/api/v1/playlists/m3u', params={**params, 'list': row.id})
    assert whole.status_code == listed.status_code == 200
    for tv in t.values():
        assert f'/tuner/channel/{tv.id}.ts' in whole.text
    assert 'group-title="Beta; Alpha"' in whole.text
    assert f'/tuner/channel/{t["One"].id}.ts' in listed.text
    assert f'/tuner/channel/{t["Two"].id}.ts' in listed.text
    assert f'/tuner/channel/{t["Three"].id}.ts' not in listed.text
    assert f'/tuner/channel/{t["Four"].id}.ts' not in listed.text
    assert 'group-title="Beta' not in listed.text


@pytest.mark.parametrize('path', ['/api/v1/playlists/tv-channels/m3u', '/api/v1/playlists/all-streams/m3u',
                                  '/playlists/m3u'])
def test_every_m3u_route_takes_the_list(client, db_session, catalogue, path):
    g, t = catalogue['groups'], catalogue['tvs']
    row = _playlist(db_session, disabled_groups=[g['alpha'].id])
    text = client.get(path, params={'base_url': RELAY, 'only_online': 'false', 'list': row.id}).text
    assert f'/tuner/channel/{t["Three"].id}.ts' in text
    assert f'/tuner/channel/{t["One"].id}.ts' not in text
    assert f'/tuner/channel/{t["Two"].id}.ts' not in text


@pytest.mark.parametrize('value', ['999', 'abc'])
def test_an_unknown_list_is_404(client, catalogue, value):
    assert client.get('/api/v1/playlists/m3u', params={'list': value}).status_code == 404


def test_playlist_api_round_trip(client, catalogue):
    g, t = catalogue['groups'], catalogue['tvs']
    groups = client.get('/api/v1/device-playlists/groups').json()
    assert [x['name'] for x in groups] == ['Alpha', 'Beta', 'Gamma', 'Gamma Sub']
    assert {x['name']: x['channels'] for x in groups} == {'Alpha': 2, 'Beta': 1, 'Gamma': 1, 'Gamma Sub': 1}

    created = client.post('/api/v1/device-playlists', json={'name': ' Example room '})
    assert created.status_code == 201
    body = created.json()
    assert (body['name'], body['channels'], body['total'], body['tabs']) == ('Example room', 4, 4, 4)

    pid = body['id']
    body = client.put(f'/api/v1/device-playlists/{pid}', json={
        'disabled_groups': [g['gamma'].id, 999999],
        'enabled_channels': [t['Three'].id],
        'disabled_channels': [t['Three'].id, t['One'].id],
    }).json()
    assert body['disabled_groups'] == [g['gamma'].id]
    assert body['enabled_channels'] == [t['Three'].id]
    assert body['disabled_channels'] == [t['One'].id]
    assert (body['channels'], body['tabs']) == (2, 3)

    copy = client.post('/api/v1/device-playlists', json={'name': 'Example copy', 'copy_from': pid}).json()
    assert copy['disabled_groups'] == [g['gamma'].id] and copy['channels'] == 2
    assert [x['name'] for x in client.get('/api/v1/device-playlists').json()] == ['Example room', 'Example copy']

    assert client.delete(f'/api/v1/device-playlists/{pid}').status_code == 204
    assert client.get(f'/api/v1/device-playlists/{pid}').status_code == 404


def test_device_playlists_migration_upgrade_and_downgrade(tmp_path):
    path = tmp_path / "device-playlists.sqlite"
    upgrade_to_revision(path, "20261113_1200")
    engine = create_engine(database_url_for(path))
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("device_playlists")}
        assert {"id", "name", "owner_id", "disabled_groups", "enabled_channels", "disabled_channels"} <= columns
        downgrade_to_revision(path, "20261112_1200")
        assert "device_playlists" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
