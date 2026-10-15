"""Jobs missed while the machine was off run at startup.

Interval jobs are re-created in memory on every start, so their first run
lands one whole interval after startup. On a box that is only switched on for
a few hours, `epg_refresh` (6 h) had never run while `epg_program_cleanup`
(1 h) kept deleting expired programmes: the guide emptied itself. One manual
refresh on a test bench took it from 12 programmes for the day to 26,259.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.config import ScheduleAnchors
from app.services.task_service import TaskService

AHORA = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
H = 3600


def _nada():
    return None


@pytest.fixture
def servicio():
    s = TaskService()
    s.scheduler.start(paused=True)          # nada se ejecuta de verdad
    yield s
    s.scheduler.shutdown(wait=False)


def _registra(s, job_id, segundos, last_run):
    s.add_interval_task(_nada, seconds=segundos, job_id=job_id)
    with s._state_lock:
        s._task_states[job_id]["last_run"] = last_run


def _siguiente(s, job_id):
    return s.scheduler.get_job(job_id).next_run_time


def test_LA_GUIA_QUE_NUNCA_CORRIO_SE_ADELANTA(servicio):
    """El caso de la caja: epg_refresh no habia corrido nunca."""
    _registra(servicio, "epg_refresh", 6 * H, None)
    assert servicio.catch_up_missed_runs(now=AHORA) == ["epg_refresh"]
    assert _siguiente(servicio, "epg_refresh") == AHORA


def test_VENCIDA_SE_ADELANTA(servicio):
    _registra(servicio, "epg_refresh", 6 * H, AHORA - timedelta(hours=9))
    assert servicio.catch_up_missed_runs(now=AHORA) == ["epg_refresh"]


def test_RECIENTE_NO_SE_TOCA(servicio):
    """Si corrio hace una hora, no se baja otra vez la guia al arrancar."""
    _registra(servicio, "epg_refresh", 6 * H, AHORA - timedelta(hours=1))
    antes = _siguiente(servicio, "epg_refresh")
    assert servicio.catch_up_missed_runs(now=AHORA) == []
    assert _siguiente(servicio, "epg_refresh") == antes


def test_LA_QUE_VACIA_LA_GUIA_NUNCA_SE_ADELANTA(servicio):
    """Adelantar epg_program_cleanup empeoraria lo que se viene a arreglar."""
    _registra(servicio, "epg_program_cleanup", H, None)
    assert servicio.catch_up_missed_runs(now=AHORA) == []


def test_LA_QUE_ABRE_SESIONES_DEL_MOTOR_TAMPOCO(servicio):
    """channel_status ahoga una caja pequeña si arranca con el catalogo."""
    _registra(servicio, "channel_status", 60 * 60, None)
    assert servicio.catch_up_missed_runs(now=AHORA) == []


def test_ESCALONADAS_guia_primero_y_listas_despues(servicio):
    _registra(servicio, "url_scraping", 24 * H, None)
    _registra(servicio, "epg_refresh", 6 * H, None)
    assert servicio.catch_up_missed_runs(now=AHORA) == ["epg_refresh", "url_scraping"]
    assert _siguiente(servicio, "epg_refresh") == AHORA
    assert _siguiente(servicio, "url_scraping") == AHORA + timedelta(seconds=300)


def test_CON_HORA_FIJA_DEL_USUARIO_NO_SE_TOCA(servicio):
    """Quien pide las 04:00 quiere las 04:00."""
    servicio.configure_schedule(ScheduleAnchors(epg_refresh="04:00"))
    _registra(servicio, "epg_refresh", 6 * H, None)
    assert servicio.catch_up_missed_runs(now=AHORA) == []


def test_LAST_RUN_COMO_TEXTO_tambien_vale(servicio):
    """Por si el almacen de estado lo devuelve serializado."""
    _registra(servicio, "epg_refresh", 6 * H, (AHORA - timedelta(hours=1)).isoformat())
    assert servicio.catch_up_missed_runs(now=AHORA) == []



# --- channel_status: se mira otra vez a los 15 min -------------------------
# La caja se enciende a ratos: la primera vuelta llegaba a los 60 min de
# arrancar, y cada reinicio la retrasaba otra vez. Al arrancar no, que abre el
# motor mientras se carga el catalogo y se abre el canal: a los 15 min.

def test_CHANNEL_STATUS_SE_MIRA_A_LOS_15_MIN(servicio):
    _registra(servicio, "channel_status", H, None)
    assert servicio.catch_up_later("channel_status", delay_seconds=900, now=AHORA)
    assert _siguiente(servicio, "channel_status_catch_up") == AHORA + timedelta(seconds=900)


def test_A_LOS_15_MIN_SI_SIGUE_ATRASADA_SE_LANZA(servicio, monkeypatch):
    _registra(servicio, "channel_status", H, AHORA - timedelta(hours=14))
    lanzadas = []
    monkeypatch.setattr(servicio, "run_task_now", lambda job_id: lanzadas.append(job_id) or "triggered")
    assert servicio.run_if_overdue("channel_status", now=AHORA) == "triggered"
    assert lanzadas == ["channel_status"]


def test_SI_YA_CORRIO_DESDE_QUE_ARRANCO_NO_SE_DUPLICA(servicio, monkeypatch):
    """La decision se toma al dispararse: una vuelta a mano, o un reinicio
    rapido, hace que no haga falta."""
    _registra(servicio, "channel_status", H, AHORA - timedelta(minutes=10))
    lanzadas = []
    monkeypatch.setattr(servicio, "run_task_now", lambda job_id: lanzadas.append(job_id) or "triggered")
    assert servicio.run_if_overdue("channel_status", now=AHORA) == "not_needed"
    assert lanzadas == []


def test_CON_HORA_FIJA_TAMPOCO_SE_PROGRAMA(servicio):
    servicio.configure_schedule(ScheduleAnchors(channel_status="04:00"))
    _registra(servicio, "channel_status", H, None)
    assert not servicio.catch_up_later("channel_status", delay_seconds=900, now=AHORA)
