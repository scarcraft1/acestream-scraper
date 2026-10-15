"""A source not seen online lately gets less time to start.

On the box (15-10) a channel stopped 4 s in and the relay gave 20 s to each
of two stale sources, one already known dead, while Kodi showed the last
frame. A source checked online within FRESH_CHECK_HOURS keeps the full time;
the rest get DOUBTFUL_ATTEMPT_SECONDS. One cut short is not written down as
offline: it may be slow, not dead.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

import app.services.tuner_playback_service as playback
from app.models.models import AcestreamChannel, TVChannel
from app.services.stream_relay import EngineStreamError, RelayRegistry
from app.services.tuner_service import TunerService


@pytest.fixture(autouse=True)
def clean_failures(monkeypatch):
    monkeypatch.setattr(playback, '_failures', __import__('collections').OrderedDict())


def _slow(monkeypatch, start_after):
    async def relay(_engine, content_id, _label, **kwargs):
        await asyncio.sleep(start_after)
        yield b'video'
    monkeypatch.setattr(playback, 'relay_engine_stream', relay)


async def _open(doubtful, verdicts=None):
    registry = RelayRegistry()
    claim = registry.try_open('slow', 'viewer', 1)

    async def verdict(cid, ok, note):
        verdicts.append((cid, ok, note))
    return await anext(playback.relay_ranked_streams(
        Mock(), ['slow'], 'viewer', claim, registry=registry, attempt_seconds=0.3, startup_budget_seconds=1,
        is_doubtful=lambda cid: doubtful, on_source_verdict=verdict if verdicts is not None else None))


@pytest.mark.asyncio
async def test_UNA_FRESCA_TIENE_TODO_EL_PLAZO(monkeypatch):
    monkeypatch.setattr(playback, 'DOUBTFUL_ATTEMPT_SECONDS', 0.05)
    _slow(monkeypatch, 0.15)
    assert await _open(doubtful=False) == b'video'


@pytest.mark.asyncio
async def test_UNA_DUDOSA_LENTA_SE_CORTA_ANTES_Y_NO_SE_DA_POR_MUERTA(monkeypatch):
    monkeypatch.setattr(playback, 'DOUBTFUL_ATTEMPT_SECONDS', 0.05)
    _slow(monkeypatch, 0.15)
    verdicts = []
    with pytest.raises(EngineStreamError):
        await _open(doubtful=True, verdicts=verdicts)
    assert verdicts == []


@pytest.mark.asyncio
async def test_UNA_DUDOSA_QUE_FALLA_POR_OTRA_COSA_SI_SE_APUNTA(monkeypatch):
    monkeypatch.setattr(playback, 'DOUBTFUL_ATTEMPT_SECONDS', 0.05)

    async def refused(_engine, content_id, _label, **kwargs):
        raise EngineStreamError('not found')
        yield b''
    monkeypatch.setattr(playback, 'relay_engine_stream', refused)
    verdicts = []
    with pytest.raises(EngineStreamError):
        await _open(doubtful=True, verdicts=verdicts)
    assert [(cid, ok) for cid, ok, _ in verdicts] == [('slow', False)]


def test_DUDOSAS_SON_LAS_NO_VISTAS_VIVAS_HACE_POCO(db_session):
    tv = TVChannel(name='M+ LaLiga 4', is_active=True)
    db_session.add(tv)
    db_session.flush()
    now = datetime.now(timezone.utc)
    for key, online, hours in (('a', True, 1), ('b', True, 30), ('c', None, None), ('d', False, 30)):
        db_session.add(AcestreamChannel(id=key * 40, name=key, tv_channel_id=tv.id, is_active=True, is_online=online,
                                        last_checked=None if hours is None else now - timedelta(hours=hours)))
    db_session.commit()
    service = TunerService(db_session)
    assert service.relay_doubtful_ids(tv.id) == {'b' * 40, 'c' * 40, 'd' * 40}
    assert service.relay_stream_ids(tv.id)[0] == 'a' * 40


def test_LA_RUTA_PASA_LAS_DUDOSAS():
    import inspect
    from app.api.endpoints import tuner
    source = inspect.getsource(tuner.tuner_channel)
    assert 'is_doubtful=doubtful.__contains__' in source
    assert 'doubtful.update(await run_in_threadpool(_doubtful_candidates, tv_channel_id))' in source
