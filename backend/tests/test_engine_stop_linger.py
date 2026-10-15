"""Kodi's second GET finds the source still running.

Kodi opens a channel with two GETs and closes the first. The relay stopped
the engine session as the first closed, so the second started cold: 1.8 s
more on the bench and 3.2 s on the box (kodi-0c, 15-10). When the client
leaves, the TV relay now defers the final stop; a start of the same source
within the delay cancels it. A source that fails is still stopped at once.
"""
import asyncio
import threading
from unittest.mock import Mock

import httpx
import pytest

from app.services import engine_client as engine_module
from app.services.engine_client import EngineClient, EngineRefusedError

CID = "c" * 40


@pytest.fixture
def engine(monkeypatch):
    """An engine that records starts; stops are recorded wherever they come from."""
    log = []
    stopped = threading.Event()

    def handler(request):
        if request.url.path == "/ace/getstream":
            log.append(("start", request.url.params["pid"]))
            if log and getattr(handler, "refuse", False):
                return httpx.Response(200, json={"response": None, "error": "refused"})
            return httpx.Response(200, json={"response": {
                "playback_url": "http://engine:6878/ace/r/x/tok", "stat_url": "http://engine:6878/ace/stat/x/s",
                "command_url": "http://engine:6878/ace/cmd/x/s", "is_live": 1}, "error": None})
        log.append(("stop", request.url.params.get("method")))
        stopped.set()
        return httpx.Response(200, json={"response": "ok", "error": None})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    # The deferred stop opens its own client: route it to the same fake engine.
    monkeypatch.setattr(engine_module.httpx, "Client", lambda *a, **k: real_client(transport=transport))
    client = EngineClient("http://engine-linger-%d:6878" % id(log), client=real_client(transport=transport))
    return client, log, stopped, handler


def test_SIN_GRACIA_SE_PARA_EN_EL_ACTO(engine):
    client, log, _stopped, _ = engine
    client.stop(client.start(CID))
    assert [kind for kind, _ in log] == ["start", "stop"]


def test_CON_GRACIA_SE_PARA_AL_CABO(engine):
    client, log, stopped, _ = engine
    client.stop(client.start(CID), linger_seconds=0.05)
    assert [kind for kind, _ in log] == ["start"]
    assert stopped.wait(2)
    assert [kind for kind, _ in log] == ["start", "stop"]


def test_EL_SEGUNDO_GET_CANCELA_LA_PARADA(engine):
    client, log, stopped, _ = engine
    client.stop(client.start(CID), linger_seconds=0.1)
    second = client.start(CID)
    assert not stopped.wait(0.3)          # the deferred stop never fires
    client.stop(second)
    assert [kind for kind, _ in log] == ["start", "start", "stop"]


def test_SI_EL_SEGUNDO_ARRANQUE_FALLA_NO_QUEDA_SESION_VIVA(engine):
    client, log, stopped, handler = engine
    client.stop(client.start(CID), linger_seconds=5)
    handler.refuse = True
    with pytest.raises(EngineRefusedError):
        client.start(CID)
    assert [kind for kind, _ in log] == ["start", "start", "stop"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["client_leaves", "source_stalls"])
async def test_EL_RELE_SOLO_ESPERA_CUANDO_SE_VA_EL_CLIENTE(monkeypatch, ending):
    import app.services.tuner_playback_service as playback
    from app.services.stream_relay import EngineStreamError, RelayRegistry
    lingers = []

    async def relay(_engine, content_id, _label, *, stop_linger, **kwargs):
        try:
            yield b"a"
            await asyncio.sleep(10)
            yield b"b"
        finally:
            lingers.append(stop_linger())
    monkeypatch.setattr(playback, "relay_engine_stream", relay)
    monkeypatch.setattr(playback, "STALL_SECONDS", 0.05)
    registry = RelayRegistry()
    claim = registry.try_open("one", "viewer", 1)
    iterator = playback.relay_ranked_streams(Mock(), ["one"], "viewer", claim, registry=registry,
                                             attempt_seconds=1, startup_budget_seconds=1)
    assert await anext(iterator) == b"a"
    if ending == "client_leaves":
        await iterator.aclose()
        assert lingers == [playback.STOP_LINGER_SECONDS]
    else:
        with pytest.raises((EngineStreamError, StopAsyncIteration)):
            await anext(iterator)
        assert lingers == [0]


def test_EL_RELE_PASA_LA_GRACIA_AL_MOTOR(monkeypatch):
    """relay_engine_stream hands the linger to EngineClient.stop."""
    import anyio
    from app.services.stream_relay import RelayRegistry, relay_engine_stream
    engine = Mock()
    engine.engine_url = "http://engine:6878"
    engine.start.return_value = Mock(playback_url="http://engine:6878/ace/r/x/tok")

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"ts")), **kwargs)

    async def main():
        stream = relay_engine_stream(engine, CID, "viewer", client_factory=factory, registry=RelayRegistry(),
                                     stop_linger=lambda: 5.0)
        assert await anext(stream) == b"ts"
        await stream.aclose()
    anyio.run(main)
    engine.stop.assert_called_once_with(engine.start.return_value, linger_seconds=5.0)
