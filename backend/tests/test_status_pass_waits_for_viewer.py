"""The scheduled status pass waits while someone watches.

On 2026-10-14 the hourly pass started at 21:49, in the middle of the match,
and kept opening the engine next to the channel being watched until the box
was switched off at 22:58. It only skipped the source the relay was playing.
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.services import viewing
from app.services.stream_relay import relay_registry
from app.tasks import channel_status_task


@pytest.fixture(autouse=True)
def _nobody_watching(monkeypatch):
    # A fresh shutdown flag: an earlier test that stops the app leaves the
    # process-wide one set, and then the pass stops before waiting at all.
    import threading
    from app.services.task_service import task_service
    monkeypatch.setattr(task_service, "shutdown_event", threading.Event())
    monkeypatch.setattr(viewing, "_last_live_request", float("-inf"))
    for info in relay_registry.active():
        relay_registry.close(info.id)
    yield
    for info in relay_registry.active():
        relay_registry.close(info.id)


def test_NADIE_VIENDO():
    assert viewing.watching() is None


def test_EL_RELE_DE_TV_CUENTA_MIENTRAS_DURA_SU_RESERVA():
    claim = relay_registry.try_open("", "tuner:192.168.200.64", 4)
    assert viewing.watching() == "tv relay"
    relay_registry.close(claim.id)
    assert viewing.watching() is None


def test_EL_REPRODUCTOR_WEB_CUENTA_SOLO_SI_ESTA_ARRANCANDO_O_LISTO(monkeypatch):
    from app.services.player_service import player_service
    sessions = [SimpleNamespace(state="error"), SimpleNamespace(state="stopped")]
    monkeypatch.setattr(player_service, "list_sessions", lambda: sessions)
    assert viewing.watching() is None
    sessions.append(SimpleNamespace(state="ready"))
    assert viewing.watching() == "web player"


def test_UNA_PETICION_A_LIVE_CUENTA_UN_RATO_Y_CADUCA(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(viewing.time, "monotonic", lambda: now[0])
    viewing.note_live_request()
    now[0] += viewing.LIVE_IDLE_SECONDS - 1
    assert viewing.watching() == "live"
    now[0] += 2
    assert viewing.watching() is None


def test_LIVE_APUNTA_AL_JUGADOR_Y_NO_AL_PROPIO_RELE(client, monkeypatch):
    """Kodi reading /live/ itself counts; the relay's loopback reads (?r=1)
    do not need to, its claim already does."""
    from app.config.settings import get_settings
    from app.services.tuner_network import get_tuner_gate
    monkeypatch.setenv("TUNER_ALLOWED_NETWORKS", "*")
    get_settings.cache_clear(); get_tuner_gate.cache_clear()
    seen = []
    monkeypatch.setattr(viewing, "note_live_request", lambda: seen.append(1))
    client.get("/live/url%3Anoexiste?r=1")
    assert seen == []
    client.get("/live/url%3Anoexiste")
    assert seen == [1]
    monkeypatch.delenv("TUNER_ALLOWED_NETWORKS")
    get_settings.cache_clear(); get_tuner_gate.cache_clear()


def _fast_sleep(monkeypatch):
    clock = {"t": 0.0}

    class Loop:
        def time(self):
            return clock["t"]

    async def sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(channel_status_task.asyncio, "get_running_loop", lambda: Loop())
    monkeypatch.setattr(channel_status_task.asyncio, "sleep", sleep)
    return clock


def test_ESPERA_HASTA_QUE_DEJAN_DE_VER(monkeypatch):
    clock = _fast_sleep(monkeypatch)
    answers = iter(["tv relay", "tv relay", "tv relay", None])
    monkeypatch.setattr(viewing, "watching", lambda: next(answers))
    waited, gave_up = asyncio.run(channel_status_task.wait_while_watching(logging.getLogger("t"), 3600))
    assert not gave_up
    assert waited == 3 * channel_status_task.VIEWING_POLL_SECONDS == clock["t"]


def test_SE_RINDE_TRAS_UN_INTERVALO_Y_NO_SE_QUEDA_COLGADA(monkeypatch):
    _fast_sleep(monkeypatch)
    monkeypatch.setattr(viewing, "watching", lambda: "tv relay")
    waited, gave_up = asyncio.run(channel_status_task.wait_while_watching(logging.getLogger("t"), 60))
    assert gave_up
    assert waited == 60


def test_LA_VUELTA_NO_COMPRUEBA_NADA_MIENTRAS_SE_VE(db_session, monkeypatch):
    """La vuelta entera: con alguien viendo no abre ninguna fuente."""
    from app.models.models import AcestreamChannel
    for cid in ("a", "b"):
        db_session.add(AcestreamChannel(id=cid, name=cid, is_active=True))
    db_session.commit()
    _fast_sleep(monkeypatch)
    probed = []

    class Service:
        def __init__(self, db):
            pass

        def _get_engine_url(self):
            return "http://engine.test:6878"

        async def check_channel_status(self, channel, **kwargs):
            probed.append(channel.id)
            return {"status": "checked", "is_online": True}

    monkeypatch.setattr(channel_status_task, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(channel_status_task, "ChannelStatusService", Service)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(viewing, "watching", lambda: "tv relay")
    result = channel_status_task.run_channel_status_task()
    assert probed == []
    assert result["checked"] == 0

    monkeypatch.setattr(viewing, "watching", lambda: None)
    result = channel_status_task.run_channel_status_task()
    assert sorted(probed) == ["a", "b"]
    assert result["checked"] == 2
