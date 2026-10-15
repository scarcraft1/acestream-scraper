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
    assert 'startup_budget_seconds=budget' in source


def _source_that(dies_after_chunks, tag):
    """A source that sends some chunks and then stops sending (a stall)."""
    async def relay(_engine, content_id, _label, **kwargs):
        for i in range(dies_after_chunks):
            yield f'{tag}{i}'.encode()
        await asyncio.sleep(10)
    return relay


@pytest.mark.asyncio
async def test_SI_LA_FUENTE_SE_SECA_EN_PLENA_EMISION_SIGUE_CON_OTRA(monkeypatch):
    """Óscar, 14-10: better a switch than the channel stopping."""
    import app.services.tuner_playback_service as playback
    calls = []

    async def relay(_engine, content_id, _label, **kwargs):
        calls.append(content_id)
        yield f'{content_id}-a'.encode()
        if content_id == 'first':
            await asyncio.sleep(10)        # stalls after its first chunk
        yield f'{content_id}-b'.encode()
    monkeypatch.setattr(playback, 'relay_engine_stream', relay)
    monkeypatch.setattr(playback, 'STALL_SECONDS', 0.05)
    registry = RelayRegistry()
    claim = registry.try_open('first', 'viewer', 1)
    received = []
    from app.services.stream_relay import EngineStreamError
    with pytest.raises(EngineStreamError):   # once every source is spent
        async for chunk in relay_ranked_streams(Mock(), ['first', 'second'], 'viewer', claim, registry=registry,
                                                attempt_seconds=1, startup_budget_seconds=2, switch_midstream=True):
            received.append(chunk)
    assert received == [b'first-a', b'second-a', b'second-b']
    assert calls == ['first', 'second']


@pytest.mark.asyncio
async def test_SIN_EL_AJUSTE_SE_CORTA_COMO_ANTES(monkeypatch):
    import app.services.tuner_playback_service as playback

    async def relay(_engine, content_id, _label, **kwargs):
        yield f'{content_id}-a'.encode()
        await asyncio.sleep(10)
    monkeypatch.setattr(playback, 'relay_engine_stream', relay)
    monkeypatch.setattr(playback, 'STALL_SECONDS', 0.05)
    registry = RelayRegistry()
    claim = registry.try_open('first', 'viewer', 1)
    received = [chunk async for chunk in relay_ranked_streams(Mock(), ['first', 'second'], 'viewer', claim,
                                                              registry=registry, attempt_seconds=1, startup_budget_seconds=2)]
    assert received == [b'first-a']


def test_LA_RUTA_PASA_EL_AJUSTE_DE_SALTAR():
    import inspect
    from app.api.endpoints import tuner
    assert 'switch_midstream=await run_in_threadpool(_switch_on_failure)' in inspect.getsource(tuner)


@pytest.mark.asyncio
async def test_EL_RELE_APUNTA_LO_QUE_APRENDE(monkeypatch):
    """A source that dries up is written down as offline; one that plays
    past HEALTHY_SECONDS as online. Measured on the box: a source kept
    "online" by the check only replayed old buffer and stalled each time."""
    import app.services.tuner_playback_service as playback
    verdicts = []

    async def relay(_engine, content_id, _label, **kwargs):
        yield f'{content_id}-a'.encode()
        if content_id == 'stale':
            await asyncio.sleep(10)
        for i in range(3):
            await asyncio.sleep(0.03)
            yield f'{content_id}-{i}'.encode()

    async def verdict(cid, ok, note):
        verdicts.append((cid, ok, note))
    monkeypatch.setattr(playback, 'relay_engine_stream', relay)
    monkeypatch.setattr(playback, 'STALL_SECONDS', 0.05)
    monkeypatch.setattr(playback, 'HEALTHY_SECONDS', 0.05)
    registry = RelayRegistry()
    claim = registry.try_open('stale', 'viewer', 1)
    from app.services.stream_relay import EngineStreamError
    with pytest.raises(EngineStreamError):
        async with asyncio.timeout(3):
            async for _ in relay_ranked_streams(Mock(), ['stale', 'live'], 'viewer', claim, registry=registry,
                                                attempt_seconds=1, startup_budget_seconds=2, switch_midstream=True,
                                                on_source_verdict=verdict):
                pass
    assert verdicts[0] == ('stale', False, 'TV relay: read_timeout while streaming')
    assert ('live', True, 'played') in verdicts
    # A source that already proved itself is not marked offline when it ends later.
    assert ('live', False) not in [(c, ok) for c, ok, _ in verdicts]


def test_EL_VEREDICTO_SE_GUARDA_SIN_TOCAR_LO_DEMAS(db_session):
    from app.models.models import AcestreamChannel
    from app.repositories.channel_repository import ChannelRepository
    db_session.add(AcestreamChannel(id='b' * 40, name='x', is_online=True, network_status='found', bitrate_bps=5_000_000))
    db_session.commit()
    ChannelRepository(db_session).record_playback_result('b' * 40, False, 'TV relay: read_timeout while streaming')
    row = db_session.get(AcestreamChannel, 'b' * 40)
    assert (row.is_online, row.check_error, row.network_status, row.bitrate_bps) == (
        False, 'TV relay: read_timeout while streaming', 'found', 5_000_000)
    assert row.last_checked is not None


@pytest.mark.asyncio
async def test_DOS_FUENTES_QUE_SE_ATASCAN_NO_SE_TURNAN_SIN_FIN(monkeypatch):
    """The stall wait is not playing time. Both sources send one chunk and
    stall; counting the STALL_SECONDS wait as healthy delivery reset the
    attempts after each one and the relay alternated between them forever."""
    import app.services.tuner_playback_service as playback
    calls = []

    async def relay(_engine, content_id, _label, **kwargs):
        calls.append(content_id)
        yield f'{content_id}-a'.encode()
        await asyncio.sleep(10)
    monkeypatch.setattr(playback, 'relay_engine_stream', relay)
    monkeypatch.setattr(playback, 'STALL_SECONDS', 0.05)
    monkeypatch.setattr(playback, 'HEALTHY_SECONDS', 0.04)
    registry = RelayRegistry()
    claim = registry.try_open('one', 'viewer', 1)
    from app.services.stream_relay import EngineStreamError
    with pytest.raises(EngineStreamError):
        async with asyncio.timeout(3):
            async for _ in relay_ranked_streams(Mock(), ['one', 'two'], 'viewer', claim, registry=registry,
                                                attempt_seconds=1, startup_budget_seconds=60, switch_midstream=True):
                pass
    assert calls == ['one', 'two']


def test_EL_ATASCO_ESTA_POR_ENCIMA_DEL_HUECO_DE_UNA_FUENTE_SANA():
    """Measured on the bench (kodi-0c, 15-10): a healthy acestream at 97 kB/s
    was silent up to 16.4 s between engine pieces, and the relay cut it at
    15 s. The card in mid-broadcast waits the same, so it never cuts in
    between two pieces of a healthy source."""
    import app.services.tuner_playback_service as playback
    assert playback.STALL_SECONDS > 16.4 * 1.4
