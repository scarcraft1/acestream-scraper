"""How long the relay waits for an acestream to start: a setting, not a constant.

A source got 15 s to deliver its first byte, and a cold start on the box (an
A55) took 17.4 s: those channels always failed cold, then sat out a 60 s
penalty. With the playlist's mimetype, waiting longer no longer blocks the
remote, it only delays the picture or the error. Óscar asked for it in the
interface, because the box is slower than the bank.
"""
import asyncio
from unittest.mock import Mock

import pytest

from app.services.stream_relay import RelayRegistry
from app.services.tuner_playback_service import relay_ranked_streams
from app.services.tuner_service import TunerService


@pytest.fixture(autouse=True)
def clean_failures(monkeypatch):
    monkeypatch.setattr('app.services.tuner_playback_service._failures', __import__('collections').OrderedDict())


def test_POR_DEFECTO_20_Y_28(db_session):
    """Kodi gives up opening a channel after 30 s: the whole start fits in 28,
    with room for a second source after one that fails quickly."""
    settings = TunerService(db_session).settings()
    assert (settings.source_start_seconds, settings.startup_budget_seconds) == (20, 28)


def test_SE_GUARDAN_Y_SE_ACOTAN(db_session):
    service = TunerService(db_session)
    service.update_settings(source_start_seconds=40, startup_budget_seconds=58)
    assert (service.settings().source_start_seconds, service.settings().startup_budget_seconds) == (40, 58)
    service.update_settings(source_start_seconds=1, startup_budget_seconds=9999)
    assert (service.settings().source_start_seconds, service.settings().startup_budget_seconds) == (5, 180)


def test_LA_API_LOS_ENSENA_Y_LOS_CAMBIA(client):
    assert client.get('/api/v1/tuner/settings').json()['source_start_seconds'] == 20
    response = client.put('/api/v1/tuner/settings', json={'source_start_seconds': 35, 'startup_budget_seconds': 50})
    assert response.status_code == 200
    assert (response.json()['source_start_seconds'], response.json()['startup_budget_seconds']) == (35, 50)
    assert client.put('/api/v1/tuner/settings', json={'source_start_seconds': 0}).status_code == 422


def _slow_engine(monkeypatch, start_after):
    """A source that delivers its first byte after `start_after` seconds."""
    async def relay(_engine, content_id, _label, **kwargs):
        await asyncio.sleep(start_after)
        yield b'video'
    monkeypatch.setattr('app.services.tuner_playback_service.relay_engine_stream', relay)


@pytest.mark.asyncio
async def test_UNA_FUENTE_LENTA_ARRANCA_SI_SE_LE_DA_TIEMPO(monkeypatch):
    # Scaled down: 0.15 s of start against 0.1 s (fails) or 0.3 s (plays).
    _slow_engine(monkeypatch, 0.15)
    registry = RelayRegistry()
    claim = registry.try_open('slow', 'viewer', 1)
    first = await anext(relay_ranked_streams(Mock(), ['slow'], 'viewer', claim, registry=registry,
                                             attempt_seconds=0.3, startup_budget_seconds=1))
    assert first == b'video'


@pytest.mark.asyncio
async def test_CON_EL_PLAZO_CORTO_FALLA(monkeypatch):
    """The control: the same slow source, with the old short per-source time."""
    from app.services.stream_relay import EngineStreamError
    _slow_engine(monkeypatch, 0.15)
    registry = RelayRegistry()
    claim = registry.try_open('slow', 'viewer', 1)
    with pytest.raises(EngineStreamError):
        await anext(relay_ranked_streams(Mock(), ['slow'], 'viewer', claim, registry=registry,
                                         attempt_seconds=0.05, startup_budget_seconds=1))


def test_LA_RUTA_DEL_RELE_PASA_LOS_AJUSTES(db_session, monkeypatch):
    """Read where experimental_transcoding is read, and handed to the relay."""
    import inspect
    from app.api.endpoints import tuner
    source = inspect.getsource(tuner)
    assert 'attempt_seconds=relay_settings.source_start_seconds' in source
    assert 'startup_budget_seconds=relay_settings.startup_budget_seconds' in source
