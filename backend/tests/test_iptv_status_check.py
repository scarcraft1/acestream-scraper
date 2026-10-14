"""The status check does not ask the acestream engine about IPTV streams.

An IPTV stream has an opaque `url:<sha1>` id. The engine cannot resolve it:
asked, it reports the stream missing, and the check marks it offline. On a
real box that would read the whole IPTV half of the catalogue -- 592 of 1,599
sources -- as dead.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.models import AcestreamChannel
from app.services.channel_status_service import ChannelStatusService


@pytest.fixture(autouse=True)
def isolated_queue(monkeypatch):
    from app.services.probe_queue import ProbeQueue
    monkeypatch.setattr('app.services.channel_status_service.probe_queue',
                        ProbeQueue(cooldown=0, outage_backoff=0))


@pytest.fixture
def probe(db_session):
    service = ChannelStatusService(db_session)
    service._get_engine_url = lambda: 'http://engine.test:6878'
    service._get_timeout = lambda: 0.015
    service._engine_ready = AsyncMock(return_value=True)
    # Si alguien llega a preguntar al motor, que se note: responde "no existe".
    service._fetch_engine_response = AsyncMock(
        return_value=(200, {'response': None, 'error': 'not found'}, None))
    service.channel_repository = MagicMock()
    return service


def _iptv(**kw):
    return AcestreamChannel(id='url:' + 'a' * 40, name='La 1',
                            stream_url='https://cdn.example.com/la1/master.m3u8',
                            **kw)


@pytest.mark.asyncio
async def test_NO_SE_PREGUNTA_AL_MOTOR(probe):
    await probe.check_channel_status(_iptv(is_online=True))
    probe._fetch_engine_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_NO_SE_DA_POR_MUERTO(probe):
    """Lo que tuviera, lo conserva: un si sigue siendo si."""
    r = await probe.check_channel_status(_iptv(is_online=True))
    assert r['is_online'] is True, r


@pytest.mark.asyncio
async def test_NO_SE_ESCRIBE_NADA_EN_LA_BASE(probe):
    await probe.check_channel_status(_iptv(is_online=True))
    probe.channel_repository.assert_not_called()
    assert not probe.channel_repository.method_calls, probe.channel_repository.method_calls


@pytest.mark.asyncio
async def test_SE_DICE_POR_QUE(probe):
    r = await probe.check_channel_status(_iptv())
    assert 'IPTV' in r.get('message', ''), r


@pytest.mark.asyncio
async def test_UN_ACESTREAM_SI_VA_AL_MOTOR(probe):
    """Guarda: el chequeo de siempre sigue haciendose."""
    ace = AcestreamChannel(id='1' * 40, name='Canal', is_online=None)
    await probe.check_channel_status(ace)
    probe._fetch_engine_response.assert_awaited()
