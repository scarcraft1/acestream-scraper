"""Kodi opens a channel with a HEAD and two GETs, closing the first early.

Measured on the bench by the other agent (15-10, d418b7c): once the first
GET's relay really closed (shielded cleanup), the engine got a stop for the
SECOND GET's new session 4 ms after it started, and the channel fell back to
the waiting card. Real pieces here: EngineClient against a fake engine that
records getstream/stop, relay_engine_stream, relay_ranked_streams and the
waiting-card wrapper; the server's cancellation is anyio's, and the
response's shielded close is reproduced.
"""
import asyncio
import time

import anyio
from unittest.mock import Mock
import httpx
import pytest

import app.services.tuner_playback_service as playback
from app.services import engine_client as engine_module
from app.services import waiting_card
from app.services.engine_client import EngineClient
from app.services.stream_relay import RelayRegistry

CID = "d" * 40
CARD = (bytes([0x47]) + bytes(187)) * 50


@pytest.fixture
def engine(monkeypatch):
    events = []
    t0 = time.monotonic()
    base = "http://engine:%d" % (20000 + id(events) % 20000)   # own source ownership per test

    def handler(request):
        now = round(time.monotonic() - t0, 3)
        if request.url.path == "/ace/getstream":
            pid = request.url.params["pid"]
            events.append((now, "start", pid))
            return httpx.Response(200, json={"response": {
                "playback_url": f"{base}/ace/r/x/{pid}", "stat_url": f"{base}/ace/stat/x/s",
                "command_url": f"{base}/ace/cmd/x/{pid}", "is_live": 1}, "error": None})
        events.append((now, "stop", request.url.path.rsplit("/", 1)[-1]))
        return httpx.Response(200, json={"response": "ok", "error": None})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(engine_module.httpx, "Client", lambda *a, **k: real_client(transport=transport))

    async def video(request):
        # Whole relay chunks: the reader hands on CHUNK_SIZE blocks.
        for _ in range(200):
            yield (b"\x47" + bytes(187)) * 349
            await asyncio.sleep(0.02)

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=video(r))), **kwargs)

    def new_engine():
        return EngineClient(base, client=real_client(transport=transport))
    return new_engine, factory, events


async def _kodi_get(new_engine, factory, registry, seconds):
    """One GET as the route serves it: relay + card wrapper, consumed until the
    client leaves (anyio cancellation), then the response's shielded close."""
    claim = registry.try_open(CID, "tuner:kodi", 4)
    relay = playback.relay_ranked_streams(new_engine(), [CID], "tuner:kodi", claim, registry=registry,
                                          client_factory=factory, attempt_seconds=2, startup_budget_seconds=3,
                                          switch_midstream=True)
    stream = waiting_card.with_waiting_card(relay, CARD, after=5, midstream_after=5)
    got = 0
    with anyio.move_on_after(seconds):
        async for chunk in stream:
            got += len(chunk)
    with anyio.CancelScope(shield=True):
        await stream.aclose()
    return got


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["second_opens_after_first_closed", "overlapping"])
async def test_EL_SEGUNDO_GET_NO_PIERDE_SU_SESION_CUANDO_SE_CIERRA_EL_PRIMERO(engine, monkeypatch, order):
    """Measured by the other agent, Kodi closes the first GET and then opens
    the second; the overlapping order is kept too."""
    new_engine, factory, events = engine
    monkeypatch.setattr(playback, "STOP_LINGER_SECONDS", 0.3)
    registry = RelayRegistry()
    if order == "overlapping":
        first = asyncio.ensure_future(_kodi_get(new_engine, factory, registry, 0.3))
        await asyncio.sleep(0.2)
        second = await _kodi_get(new_engine, factory, registry, 1.5)
        await first
    else:
        await _kodi_get(new_engine, factory, registry, 0.2)
        second = await _kodi_get(new_engine, factory, registry, 1.5)
    await asyncio.sleep(0.6)                       # linger and the deferred stop
    starts = [e for e in events if e[1] == "start"]
    stops = [e for e in events if e[1] == "stop"]
    assert len(starts) == 2
    # Nothing stopped while the second GET was playing...
    assert all(t >= 1.5 for t, _, _ in stops), events
    # ...and both sessions are stopped once it is gone.
    assert {pid for _, _, pid in stops} == {pid for _, _, pid in starts}, events
    assert second > 1_000_000              # the second GET played


def test_SI_NO_QUEDAN_FUENTES_A_MITAD_LA_RESPUESTA_ACABA_SIN_TRAZA(alembic_client, alembic_db_session, monkeypatch, caplog):
    """Bench, 15-10: 'No source could sustain...' went up through the response
    as an unhandled exception -- a 500 traceback in the log after the headers
    had already been sent."""
    from unittest.mock import Mock
    from app.api.endpoints import tuner
    from app.config.settings import get_settings
    from app.models.models import AcestreamChannel, TVChannel
    from app.services.stream_relay import EngineStreamError
    from app.services.tuner_network import get_tuner_gate
    monkeypatch.setenv('TUNER_ALLOWED_NETWORKS', '*')
    get_settings.cache_clear(); get_tuner_gate.cache_clear()
    try:
        tv = TVChannel(name='Se rinde', is_active=True)
        alembic_db_session.add(tv)
        alembic_db_session.flush()
        alembic_db_session.add(AcestreamChannel(id='e' * 40, name='x', tv_channel_id=tv.id, is_active=True, is_online=True))
        alembic_db_session.commit()
        monkeypatch.setattr(tuner.tuner_probe_service, 'start', Mock())
        monkeypatch.setattr(tuner, '_engine', lambda: Mock())

        async def ranked(engine, content_ids, label, claim, **kwargs):
            # Like the real relay, it releases the tuner slot it was given:
            # left in the process-wide registry, later tests saw someone
            # watching (the status pass paused 10 min, twice).
            try:
                yield b'\x47' + bytes(187)
                raise EngineStreamError('No source could sustain TV channel playback within the recovery budget')
            finally:
                tuner.relay_registry.close(claim.id)
        monkeypatch.setattr(tuner, 'relay_ranked_streams', ranked)
        monkeypatch.setattr(tuner, '_setting_on', lambda key: False)
        caplog.set_level('INFO')
        response = alembic_client.get(f'/tuner/channel/{tv.id}.ts')
        assert response.status_code == 200 and response.content.startswith(b'\x47')
        assert 'no source left' in caplog.text
        assert tuner.relay_registry.active() == []
    finally:
        monkeypatch.undo()
        get_settings.cache_clear(); get_tuner_gate.cache_clear()


# --- an engine session that is empty at once ---------------------------------

def _empty_then(monkeypatch, outcomes, delay=0):
    calls = []

    async def relay(_engine, content_id, _label, **kwargs):
        calls.append(content_id)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        await asyncio.sleep(delay)
        if outcome == 'empty':
            return
        yield b'video'
    monkeypatch.setattr(playback, 'relay_engine_stream', relay)
    return calls


@pytest.mark.asyncio
async def test_UNA_SESION_VACIA_AL_ARRANCAR_SE_REINTENTA_UNA_VEZ(monkeypatch):
    """Bench, 13:32 (intermittent): the engine answered 200 and closed at once
    for the second GET's session; with one source the channel fell back to
    the card."""
    calls = _empty_then(monkeypatch, ['empty', 'video'])
    registry = RelayRegistry()
    claim = registry.try_open(CID, 'kodi', 1)
    first = await anext(playback.relay_ranked_streams(Mock(), [CID], 'kodi', claim,
                                                      registry=registry, attempt_seconds=1, startup_budget_seconds=2))
    assert first == b'video' and calls == [CID, CID]


@pytest.mark.asyncio
async def test_SOLO_UNA_VEZ(monkeypatch):
    from app.services.stream_relay import EngineStreamError
    calls = _empty_then(monkeypatch, ['empty'])
    registry = RelayRegistry()
    claim = registry.try_open(CID, 'kodi', 1)
    with pytest.raises(EngineStreamError):
        await anext(playback.relay_ranked_streams(Mock(), [CID], 'kodi', claim,
                                                  registry=registry, attempt_seconds=1, startup_budget_seconds=2))
    assert calls == [CID, CID]


@pytest.mark.asyncio
async def test_UNA_QUE_TARDA_EN_ACABARSE_NO_SE_REINTENTA(monkeypatch):
    from app.services.stream_relay import EngineStreamError
    monkeypatch.setattr(playback, 'EMPTY_START_RETRY_SECONDS', 0.05)
    calls = _empty_then(monkeypatch, ['empty', 'video'], delay=0.1)
    registry = RelayRegistry()
    claim = registry.try_open(CID, 'kodi', 1)
    with pytest.raises(EngineStreamError):
        await anext(playback.relay_ranked_streams(Mock(), [CID], 'kodi', claim,
                                                  registry=registry, attempt_seconds=1, startup_budget_seconds=2))
    assert calls == [CID]
