"""A Sincroguia channel page, turned into an XMLTV guide.

The sample is the real 7 TV La Rioja page fetched on 2026-10-14, trimmed to
the title, the canonical URL and the schedule (three days, 50 programmes).
"""
import time
from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from app.services import sincroguia_guide as sg

SAMPLE = (Path(__file__).parent / "fixtures" / "sincroguia_la-7-rioja_sample.html").read_bytes()
URL = "https://sincroguia-tv.expansion.com/programacion/la-7-rioja"
TODAY = date(2026, 10, 14)


def _page(day_title, *rows):
    """A minimal page: one day title and (start, end, name) rows."""
    parts = ['<link rel="canonical" href="%s">' % URL,
             '<h1 class="m-top__title">Programación La 7 Rioja</h1>',
             '<h2 class="m-schedule-day__title">%s</h2>' % day_title]
    for i, (start, end, name) in enumerate(rows):
        parts.append('<div class="m-program" id="event-%d"><h2 class="m-program__name">%s</h2>'
                     '<p class="m-program__info">%s a %sh</p></div>' % (i, name, start, end))
    return "\n".join(parts).encode("utf-8")


def _programmes(xml):
    tv = ElementTree.fromstring(xml)
    return [(p.get("start"), p.get("stop"), p.findtext("title")) for p in tv.iter("programme")]


def test_LA_MUESTRA_REAL_da_los_tres_dias_y_los_50_programas():
    xml = sg.html_to_xmltv(SAMPLE, URL, today=TODAY)
    progs = _programmes(xml)
    assert len(progs) == 50
    assert {p[0][:8] for p in progs} >= {"20261014", "20261015", "20261016"}


def test_EL_CANAL_lleva_prefijo_propio_y_el_nombre_de_la_pagina():
    tv = ElementTree.fromstring(sg.html_to_xmltv(SAMPLE, URL, today=TODAY))
    ch = tv.find("channel")
    assert ch.get("id") == "sincroguia.la-7-rioja"
    assert ch.findtext("display-name") == "La 7 Rioja"
    assert {p.get("channel") for p in tv.iter("programme")} == {"sincroguia.la-7-rioja"}


def test_LA_HORA_ES_DE_ESPANA_Y_SALE_EN_UTC_en_verano(monkeypatch):
    """12:00 in Madrid in October (before the 25th) is 10:00 UTC. The test
    forces TZ=Europe/Madrid because the test container runs in UTC, where a
    'local time' mistake is invisible."""
    monkeypatch.setenv("TZ", "Europe/Madrid")
    time.tzset()
    try:
        xml = sg.html_to_xmltv(_page("miércoles 14 de octubre", ("12:00", "13:00", "7 Magazine")), URL, today=TODAY)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert _programmes(xml) == [("20261014100000 +0000", "20261014110000 +0000", "7 Magazine")]


def test_EN_INVIERNO_el_desfase_es_una_hora_no_dos():
    """The module this was ported from wrote a fixed +0200. In November
    12:00 Madrid is 11:00 UTC; with the fixed offset it would say 10:00."""
    xml = sg.html_to_xmltv(_page("lunes 16 de noviembre", ("12:00", "13:00", "Noticias")), URL, today=TODAY)
    assert _programmes(xml) == [("20261116110000 +0000", "20261116120000 +0000", "Noticias")]


def test_EL_CAMBIO_DE_HORA_DEL_25_DE_OCTUBRE():
    """Clocks go back at 03:00 on Sunday 2026-10-25. A programme from 01:00
    to 04:00 local starts in summer time (23:00 UTC on the 24th) and ends in
    winter time (03:00 UTC): four real hours, not three."""
    xml = sg.html_to_xmltv(_page("sábado 24 de octubre",
                                 ("23:00", "01:00", "Noche"), ("01:00", "04:00", "Madrugada")),
                           URL, today=TODAY)
    assert _programmes(xml) == [
        ("20261024210000 +0000", "20261024230000 +0000", "Noche"),
        ("20261024230000 +0000", "20261025030000 +0000", "Madrugada"),
    ]


def test_SI_LA_HORA_RETROCEDE_se_ha_cruzado_la_medianoche():
    """A schedule day runs 09:00 to 08:59: 23:30 then 00:30 is the next day."""
    xml = sg.html_to_xmltv(_page("jueves 15 de octubre",
                                 ("23:30", "00:30", "Cine"), ("00:30", "01:30", "Música")),
                           URL, today=TODAY)
    starts = [p[0] for p in _programmes(xml)]
    assert starts == ["20261015213000 +0000", "20261015223000 +0000"]
    stops = [p[1] for p in _programmes(xml)]
    assert stops[0] == "20261015223000 +0000"       # ends past midnight on its own


def test_UN_TITULO_DE_ENERO_LEIDO_EN_DICIEMBRE_es_del_ano_que_viene():
    xml = sg.html_to_xmltv(_page("sábado 2 de enero", ("10:00", "11:00", "Año nuevo")), URL,
                           today=date(2026, 12, 30))
    assert _programmes(xml)[0][0].startswith("20270102")


def test_SE_RECONOCE_LA_URL():
    assert sg.is_sincroguia_url(URL)
    assert sg.is_sincroguia_url("https://SINCROGUIA-TV.expansion.com/programacion/x")
    assert not sg.is_sincroguia_url("https://www.futbolenlatv.es/deporte/futbol")
    assert not sg.is_sincroguia_url("https://sincroguia-tv.expansion.com.evil.example/x")
    assert not sg.is_sincroguia_url("")


def test_CARACTERES_RAROS_no_rompen_el_xml():
    xml = sg.html_to_xmltv(_page("jueves 15 de octubre", ("10:00", "11:00", "Tom &amp; Jerry <b>HD</b>")),
                           URL, today=TODAY)
    assert _programmes(xml)[0][2] == "Tom & Jerry HD"


def test_SIN_FECHA_RECONOCIBLE_no_inventa_programas():
    assert _programmes(sg.html_to_xmltv(_page("hoy", ("10:00", "11:00", "X")), URL, today=TODAY)) == []


def test_LA_PAGINA_ENTRA_POR_EL_CAMINO_DE_SIEMPRE(monkeypatch):
    from types import SimpleNamespace
    from app.services import epg_service
    from app.services import epg_sources as epg_module

    class Resp:
        status_code = 200
        headers = {}
        content = SAMPLE
        is_redirect = is_permanent_redirect = False

        def raise_for_status(self):
            pass

    monkeypatch.setattr(epg_module, "source_get", lambda url, timeout=None, allow_redirects=False: Resp())
    seen = {}
    service = epg_service.EPGService(db=None)
    monkeypatch.setattr(service, "_process_epg_xml",
                        lambda source_id, content: seen.setdefault("xml", content) and (0, 0))
    r = service._fetch_epg_from_source(SimpleNamespace(
        id=1, url="https://sincroguia-tv.expansion.com/programacion/la-7-rioja"))
    assert r["success"] is True, r
    assert seen["xml"].startswith(b"<?xml") and b"<programme " in seen["xml"]
