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

        async def ranked(*args, **kwargs):
            yield b'\x47' + bytes(187)
            raise EngineStreamError('No source could sustain TV channel playback within the recovery budget')
        monkeypatch.setattr(tuner, 'relay_ranked_streams', ranked)
        monkeypatch.setattr(tuner, '_setting_on', lambda key: False)
        caplog.set_level('INFO')
        response = alembic_client.get(f'/tuner/channel/{tv.id}.ts')
        assert response.status_code == 200 and response.content.startswith(b'\x47')
        assert 'no source left' in caplog.text
    finally:
        monkeypatch.undo()
        get_settings.cache_clear(); get_tuner_gate.cache_clear()
