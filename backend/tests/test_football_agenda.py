"""Football fixtures from futbolenlatv.es, turned into an XMLTV guide.

No general guide places a match on the channel that airs it: of 42 matches on
one day, zero landed on a Spanish DAZN or M+ channel from 17 guides. The
fixtures page says which channel airs each one.

The sample is three real rows of the page (2026-10-14).
"""
import re
from pathlib import Path
from types import SimpleNamespace

from app.services import epg_service
from app.services import epg_sources as epg_module
from app.services.football_agenda import (
    broadcaster_key, events_from_html, html_to_xmltv, is_agenda_url)

SAMPLE = (Path(__file__).parent / "fixtures" / "futbolenlatv_sample.html").read_text(encoding="utf-8")


def _xml():
    return html_to_xmltv(SAMPLE.encode("utf-8")).decode("utf-8")


def _channels(x):
    return dict(re.findall(r'<channel id="([^"]+)"><display-name lang="es">([^<]+)', x))


def test_LA_HORA_ES_UTC_aunque_la_pagina_no_lo_diga(monkeypatch):
    """Trap 1: `2026-10-13T23:00:00` is UTC. Read as local it shifts 2 h.

    The local zone is forced to Madrid: in a UTC container (CI, this suite)
    reading it as local or as UTC gives the same instant, and the test would
    pass with the bug. On a box in Spain it would not.
    """
    import time
    monkeypatch.setenv("TZ", "Europe/Madrid")
    time.tzset()
    try:
        _assert_utc()
    finally:
        monkeypatch.undo()
        time.tzset()


def _assert_utc():
    ev = {e["title"]: e for e in events_from_html(SAMPLE)}
    assert ev["Estados Unidos - España"]["start"].isoformat() == "2026-10-13T23:00:00+00:00"
    assert 'start="20261013230000 +0000"' in _xml()


def test_SE_LEEN_TODAS_LAS_EMISORAS_de_cada_partido():
    """Trap 2: several <li> with different classes; filtering by class kept one."""
    ev = {e["title"]: e for e in events_from_html(SAMPLE)}
    assert len(ev["Deportivo - Levante"]["channels"]) == 5, ev["Deportivo - Levante"]


def test_DAZN_A_SECAS_NO_SE_COLOCA():
    """204 «DAZN» against 8 numbered in three weeks: choosing would be inventing."""
    assert "futbolenlatv.dazn" not in _channels(_xml())
    assert broadcaster_key("DAZN (Ver en directo)") is None


def test_POR_CONFIRMAR_NO_ES_UN_CANAL():
    assert broadcaster_key("Canal por confirmar") is None
    assert "España - México" not in _xml()


def test_DOS_NOMBRES_DE_LA_MISMA_EMISORA_SON_UN_CANAL_y_el_partido_sale_una_vez():
    """«M+ LALIGA» and «M+ LALIGA HDR» both air it; it must not appear twice."""
    x = _xml()
    assert "futbolenlatv.m-plus-laliga" in _channels(x)
    assert "futbolenlatv.m-plus-laliga-hdr" not in _channels(x)
    assert x.count('channel="futbolenlatv.m-plus-laliga"') == 1


def test_EL_NOMBRE_VISIBLE_VA_LIMPIO():
    """Without the dial number in parentheses."""
    assert _channels(_xml())["futbolenlatv.m-plus-laliga"] == "M+ LALIGA"


def test_SE_RECONOCE_LA_URL_DE_LA_AGENDA():
    assert is_agenda_url("https://www.futbolenlatv.es/deporte/futbol")
    assert not is_agenda_url("https://epgshare01.online/epg.xml.gz")
    assert not is_agenda_url("https://futbolenlatv.es.evil.com/x")


def test_LA_FUENTE_ENTRA_POR_EL_CAMINO_DE_SIEMPRE(monkeypatch):
    """Added as an ordinary guide source; the HTML is converted on the way in."""
    class Resp:
        status_code = 200
        headers = {}
        content = SAMPLE.encode("utf-8")
        is_redirect = is_permanent_redirect = False

        def raise_for_status(self):
            pass

    monkeypatch.setattr(epg_module, "source_get", lambda url, timeout=None, allow_redirects=False: Resp())
    seen = {}
    service = epg_service.EPGService(db=None)
    monkeypatch.setattr(service, "_process_epg_xml",
                        lambda source_id, content: seen.setdefault("xml", content) and (0, 0))
    r = service._fetch_epg_from_source(
        SimpleNamespace(id=1, url="https://www.futbolenlatv.es/deporte/futbol"))
    assert r["success"] is True, r
    assert seen["xml"].startswith(b"<?xml"), seen["xml"][:60]
    assert b"futbolenlatv.m-plus-laliga" in seen["xml"]


def test_UNA_GUIA_NORMAL_NO_SE_TOCA(monkeypatch):
    """Guard: XMLTV sources are passed through untouched."""
    raw = b"<tv><channel id='a'/></tv>"

    class Resp:
        status_code = 200
        headers = {}
        content = raw
        is_redirect = is_permanent_redirect = False

        def raise_for_status(self):
            pass

    monkeypatch.setattr(epg_module, "source_get", lambda url, timeout=None, allow_redirects=False: Resp())
    seen = {}
    service = epg_service.EPGService(db=None)
    monkeypatch.setattr(service, "_process_epg_xml",
                        lambda source_id, content: seen.setdefault("xml", content) and (0, 0))
    service._fetch_epg_from_source(SimpleNamespace(id=1, url="https://example.com/guide.xml"))
    assert seen["xml"] == raw
