"""TV Rioja's day pages, turned into an XMLTV guide.

The sample is the real `dia=3` (Wednesday) page fetched on 2026-10-14. The
page never states its date: it comes from the URL.
"""
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

from app.services import tvrioja_guide as tv

SAMPLE = (Path(__file__).parent / "fixtures" / "tvrioja_dia3_sample.html").read_bytes()
BASE = "https://proyectos.larioja.com/tvr/dimeprogramacion/dimeprogramacion.php"
WEDNESDAY = date(2026, 10, 14)


def _page(*rows):
    return "".join('<tr><td class="hora">%s</td><td><strong class="programa">%s</strong>%s</td></tr>'
                   % (h, t, s) for h, t, s in rows).encode("utf-8")


def _programmes(xml):
    root = ElementTree.fromstring(xml)
    return [(p.get("start"), p.get("stop"), p.findtext("title")) for p in root.iter("programme")]


def test_LA_MUESTRA_REAL_da_27_programas_del_dia_de_su_URL():
    progs = _programmes(tv.html_to_xmltv(SAMPLE, BASE + "?dia=3", today=WEDNESDAY))
    assert len(progs) == 27
    # 00:00 Madrid on 14 October (summer time) is 22:00 UTC on the 13th.
    assert progs[0] == ("20261013220000 +0000", "20261013223000 +0000", "Saber y catar")


def test_SIN_HORA_DE_FIN_acaba_cuando_empieza_el_siguiente_y_el_ultimo_a_medianoche():
    progs = _programmes(tv.html_to_xmltv(_page(("08:30h", "De un vistazo", ""), ("23:00h", "Cierre", "")),
                                         BASE + "?dia=3", today=WEDNESDAY))
    assert progs == [("20261014063000 +0000", "20261014210000 +0000", "De un vistazo"),
                     ("20261014210000 +0000", "20261014220000 +0000", "Cierre")]


def test_LA_HORA_ES_DE_ESPANA_aunque_el_proceso_corra_en_Madrid(monkeypatch):
    """The test container runs in UTC and hides local-time mistakes."""
    monkeypatch.setenv("TZ", "Europe/Madrid")
    time.tzset()
    try:
        progs = _programmes(tv.html_to_xmltv(_page(("12:00h", "Mediodía", "")), BASE + "?dia=3", today=WEDNESDAY))
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert progs[0][0] == "20261014100000 +0000"


def test_EN_INVIERNO_el_desfase_es_una_hora():
    """The ported module wrote a fixed +0200; in November that is an hour off."""
    progs = _programmes(tv.html_to_xmltv(_page(("12:00h", "Mediodía", "")), BASE + "?dia=1",
                                         today=date(2026, 11, 16)))
    assert progs[0][0] == "20261116110000 +0000"


def test_EL_CAMBIO_DE_HORA_DEL_25_DE_OCTUBRE():
    """Sunday 2026-10-25: a programme 01:00-04:00 local is four real hours
    (summer start, winter end), and the next one starts at 03:00 UTC."""
    progs = _programmes(tv.html_to_xmltv(_page(("01:00h", "Madrugada", ""), ("04:00h", "Mañana", "")),
                                         BASE + "?dia=7", today=date(2026, 10, 22)))
    assert progs[0] == ("20261024230000 +0000", "20261025030000 +0000", "Madrugada")
    assert progs[1][0] == "20261025030000 +0000"


def test_DIA_ES_LA_PROXIMA_VEZ_que_cae_ese_dia_de_la_semana_hoy_incluido():
    assert tv.page_date(BASE + "?dia=3", WEDNESDAY) == WEDNESDAY           # today
    assert tv.page_date(BASE + "?dia=4", WEDNESDAY) == date(2026, 10, 15)  # tomorrow
    assert tv.page_date(BASE + "?dia=1", WEDNESDAY) == date(2026, 10, 19)  # next Monday
    assert tv.page_date(BASE + "?dia=0", WEDNESDAY) == WEDNESDAY
    assert tv.page_date(BASE + "?dia=9", WEDNESDAY) is None
    assert tv.page_date(BASE, WEDNESDAY) == WEDNESDAY


def test_UNA_FUENTE_SON_LAS_SIETE_PAGINAS_sin_la_de_hoy_repetida():
    urls = tv.page_urls(BASE + "?dia=0")
    assert [parse_qs(urlparse(u).query)["dia"] for u in urls] == [[str(n)] for n in range(1, 8)]
    assert all(u.startswith(BASE + "?") for u in urls)


def test_SIN_FECHA_no_inventa_programas():
    assert _programmes(tv.html_to_xmltv(SAMPLE, BASE + "?dia=x", today=WEDNESDAY)) == []


def test_SE_RECONOCE_LA_URL_y_solo_esa_pagina():
    assert tv.is_tvrioja_url(BASE + "?dia=0")
    assert not tv.is_tvrioja_url("https://proyectos.larioja.com/otra/cosa.php")
    assert not tv.is_tvrioja_url("https://proyectos.larioja.com.evil.example" + BASE[len("https://proyectos.larioja.com"):])
    assert not tv.is_tvrioja_url("")


def test_EL_CANAL_lleva_prefijo_propio():
    root = ElementTree.fromstring(tv.html_to_xmltv(SAMPLE, BASE + "?dia=3", today=WEDNESDAY))
    assert root.find("channel").get("id") == "tvrioja.tvr"
    assert {p.get("channel") for p in root.iter("programme")} == {"tvrioja.tvr"}


# --- entra como cualquier fuente de guia -------------------------------------

def test_LA_SEMANA_ENTRA_COMO_UNA_SOLA_GUIA(monkeypatch):
    """Una fuente, siete paginas (dia=1..7) y un solo documento: el canal una
    vez y los programas de los siete dias. Procesarlas por separado haria que
    cada pagina pisara la anterior."""
    from types import SimpleNamespace
    from app.services import epg_service
    from app.services import epg_sources as epg_module
    pedidas = []

    class Resp:
        status_code = 200
        headers = {}
        content = SAMPLE
        is_redirect = is_permanent_redirect = False

        def raise_for_status(self):
            pass

    def get(url, timeout=None, allow_redirects=False):
        pedidas.append(url)
        return Resp()

    monkeypatch.setattr(epg_module, "source_get", get)
    seen = {}
    service = epg_service.EPGService(db=None)
    monkeypatch.setattr(service, "_process_epg_xml",
                        lambda source_id, content: seen.setdefault("xml", content) and (0, 0))
    url = "https://proyectos.larioja.com/tvr/dimeprogramacion/dimeprogramacion.php?dia=0"
    r = service._fetch_epg_from_source(SimpleNamespace(id=1, url=url))
    assert r["success"] is True, r
    assert sorted(u.rsplit("dia=", 1)[1] for u in pedidas[1:]) == [str(n) for n in range(1, 8)]
    xml = seen["xml"]
    assert xml.count(b'<channel id="tvrioja.tvr"') == 1
    por_pagina = len(tv.rows(SAMPLE.decode("utf-8", "replace")))
    assert xml.count(b"<programme ") == 7 * por_pagina
