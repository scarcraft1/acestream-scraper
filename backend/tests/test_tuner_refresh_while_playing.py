"""The TV relay can skip checking a channel's other sources while it plays."""
import asyncio
from unittest.mock import Mock

import pytest

from app.api.endpoints import tuner


@pytest.mark.parametrize('stored, expected', [(None, True), ('true', True), ('false', False), ('0', False)])
def test_EL_INTERRUPTOR_SE_LEE_DE_LOS_AJUSTES(db_session, monkeypatch, stored, expected):
    from app.repositories.settings_repository import SettingsRepository
    monkeypatch.setattr(tuner, 'SessionLocal', lambda: db_session)
    monkeypatch.setattr(db_session, 'close', lambda: None)
    if stored is not None:
        SettingsRepository(db_session).set_setting('tuner_refresh_while_playing', stored)
    assert tuner._refresh_while_playing() is expected


def test_APAGADO_NO_SE_COMPRUEBA_NADA_AL_ABRIR(monkeypatch):
    started = []
    monkeypatch.setattr(tuner.tuner_probe_service, 'start', lambda tv_id: started.append(tv_id))
    monkeypatch.setattr(tuner, '_refresh_while_playing', lambda: False)
    monkeypatch.setattr(tuner, '_channel_candidates', lambda *a, **k: ['a' * 40])
    monkeypatch.setattr(tuner, '_tuner_count', lambda: 4)
    monkeypatch.setattr(tuner, '_engine', lambda: Mock())

    async def no_source(*a, **k):
        raise tuner.EngineStreamError('nothing')
        yield b''
    monkeypatch.setattr(tuner, 'relay_ranked_streams', no_source)
    # With no source left the route sends the end card (16-10) instead of a
    # 502; read to the end, which also releases the tuner slot.
    monkeypatch.setattr(tuner.waiting_card, 'END_CARD_SECONDS', 0.05)
    request = Mock(method='GET', client=Mock(host='127.0.0.1'))

    async def open_and_read():
        response = await tuner.tuner_channel(1, request)
        async for _ in response.body_iterator:
            pass
    asyncio.run(open_and_read())
    assert started == []
