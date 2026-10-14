"""Football matches laid over a TV channel's regular guide.

A TV channel takes its guide from ONE guide channel, and the football agenda
is not a full schedule: just matches. So a channel gets a second, optional
`agenda_epg_id`, and the export lays the matches over the regular guide.
Where a match is on, it wins; the rest of the day keeps the usual programmes.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models.models import EPGChannel, EPGProgram, EPGSource, TVChannel
from app.services.epg_export import overlay_matches
from app.services.epg_service import EPGService
from app.services.playlist_service import PlaylistService

H = timedelta(hours=1)


def _p(start, end, title):
    return SimpleNamespace(start_time=start, end_time=end, title=title,
                           subtitle=None, description=None, category=None, image_url=None)


T = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + 2 * H


class TestOverlay:
    def test_EL_PARTIDO_GANA_Y_EL_BLOQUE_SE_RECORTA_no_se_pierde(self):
        """A 3 h block with a 2 h match inside keeps its remaining hour."""
        out = overlay_matches([_p(T, T + 3 * H, "Relleno")], [_p(T + H, T + 3 * H, "Celta - Málaga")])
        assert [(p.title, p.start_time - T, p.end_time - T) for p in out] == [
            ("Relleno", 0 * H, 1 * H), ("Celta - Málaga", 1 * H, 3 * H)]

    def test_UN_PARTIDO_EN_MEDIO_PARTE_EL_BLOQUE_EN_DOS(self):
        out = overlay_matches([_p(T, T + 4 * H, "Relleno")], [_p(T + H, T + 2 * H, "Partido")])
        assert [p.title for p in out] == ["Relleno", "Partido", "Relleno"]

    def test_TAPADO_ENTERO_DESAPARECE(self):
        out = overlay_matches([_p(T + H, T + 2 * H, "Relleno")], [_p(T, T + 3 * H, "Partido")])
        assert [p.title for p in out] == ["Partido"]

    def test_UN_RESTO_DE_SEGUNDOS_NO_SE_PINTA(self):
        out = overlay_matches([_p(T, T + H + timedelta(seconds=30), "Relleno")],
                              [_p(T, T + H, "Partido")])
        assert [p.title for p in out] == ["Partido"]

    def test_SIN_PARTIDOS_TODO_SIGUE_IGUAL(self):
        """Guard: channels without an agenda export exactly as before."""
        reg = [_p(T, T + H, "A"), _p(T + H, T + 2 * H, "B")]
        assert overlay_matches(reg, []) == reg

    def test_NO_SE_TOCA_LO_GUARDADO(self):
        """Programmes are copied, never modified."""
        block = _p(T, T + 3 * H, "Relleno")
        overlay_matches([block], [_p(T + H, T + 2 * H, "Partido")])
        assert (block.start_time, block.end_time) == (T, T + 3 * H)


def _source(db, name, url):
    s = EPGSource(name=name, url=url, enabled=True)
    db.add(s)
    db.flush()
    return s


def _guide_channel(db, source, xml_id, programmes):
    c = EPGChannel(epg_source_id=source.id, channel_xml_id=xml_id, name=xml_id)
    db.add(c)
    db.flush()
    for start, end, title in programmes:
        db.add(EPGProgram(epg_channel_id=c.id, start_time=start, end_time=end, title=title))
    return c


def _export(db):
    return EPGService(db).generate_epg_xml()


class TestExport:
    def test_LOS_PARTIDOS_SALEN_EN_LA_GUIA_DEL_CANAL(self, db_session):
        regular = _source(db_session, "ES1", "https://guia.example/es1.xml")
        agenda = _source(db_session, "Agenda", "https://www.futbolenlatv.es/deporte/futbol")
        _guide_channel(db_session, regular, "MLaLiga.es", [(T, T + 4 * H, "Relleno")])
        _guide_channel(db_session, agenda, "futbolenlatv.m-plus-laliga",
                       [(T + H, T + 3 * H, "Celta - Málaga")])
        db_session.add(TVChannel(name="M+ LaLiga", epg_id="MLaLiga.es", epg_source_id=regular.id,
                                 agenda_epg_id="futbolenlatv.m-plus-laliga"))
        db_session.commit()
        x = _export(db_session)
        assert "Celta - Málaga" in x, x
        assert x.count("<title>Relleno</title>") == 2, x     # before and after the match

    def test_UN_CANAL_SOLO_CON_AGENDA_TAMBIEN_SALE(self, db_session):
        """«M+ Liga de Campeones 7» may have no regular guide at all."""
        agenda = _source(db_session, "Agenda", "https://www.futbolenlatv.es/deporte/futbol")
        _guide_channel(db_session, agenda, "futbolenlatv.m-plus-liga-de-campeones-7",
                       [(T, T + 2 * H, "Galatasaray - Barcelona")])
        db_session.add(TVChannel(name="M+ Liga de Campeones 7",
                                 agenda_epg_id="futbolenlatv.m-plus-liga-de-campeones-7"))
        db_session.commit()
        x = _export(db_session)
        assert '<channel id="futbolenlatv.m-plus-liga-de-campeones-7">' in x, x
        assert "Galatasaray - Barcelona" in x, x

    def test_LA_AGENDA_DESACTIVADA_NO_SE_EXPORTA(self, db_session):
        agenda = _source(db_session, "Agenda", "https://www.futbolenlatv.es/deporte/futbol")
        agenda.enabled = False
        _guide_channel(db_session, agenda, "futbolenlatv.dazn-laliga", [(T, T + 2 * H, "Partido")])
        db_session.add(TVChannel(name="DAZN LaLiga", agenda_epg_id="futbolenlatv.dazn-laliga"))
        db_session.commit()
        assert "Partido" not in _export(db_session)


class TestPlaylistId:
    def test_SOLO_AGENDA_LLEVA_EL_ID_DE_LA_AGENDA(self, db_session):
        """Must match the id the export emits, or Kodi shows no guide."""
        svc = PlaylistService(db_session)
        tv = SimpleNamespace(epg_id=None, epg_source_id=None,
                             agenda_epg_id="futbolenlatv.m-plus-liga-de-campeones-7")
        assert svc._tv_channel_epg_id(tv, "otro") == "futbolenlatv.m-plus-liga-de-campeones-7"

    def test_CON_GUIA_NORMAL_MANDA_LA_NORMAL(self, db_session):
        svc = PlaylistService(db_session)
        tv = SimpleNamespace(epg_id="MLaLiga.es", epg_source_id=None,
                             agenda_epg_id="futbolenlatv.m-plus-laliga")
        assert svc._tv_channel_epg_id(tv, "otro") != "futbolenlatv.m-plus-laliga"


class TestClearing:
    def test_VACIAR_EL_CAMPO_QUITA_LA_AGENDA(self, client, db_session):
        tv = TVChannel(name="DAZN LaLiga", agenda_epg_id="futbolenlatv.dazn-laliga")
        db_session.add(tv)
        db_session.commit()
        r = client.put(f"/api/v1/tv-channels/{tv.id}", json={"agenda_epg_id": ""})
        assert r.status_code == 200, r.text
        db_session.refresh(tv)
        assert tv.agenda_epg_id is None

    def test_Y_CON_NULL_TAMBIEN(self, client, db_session):
        tv = TVChannel(name="DAZN LaLiga 2", agenda_epg_id="futbolenlatv.dazn-laliga")
        db_session.add(tv)
        db_session.commit()
        r = client.put(f"/api/v1/tv-channels/{tv.id}", json={"agenda_epg_id": None})
        assert r.status_code == 200, r.text
        db_session.refresh(tv)
        assert tv.agenda_epg_id is None
