"""The name a TV channel is born with when an import creates it from a guide.

TDTChannels' guide uses the id as display name (`Telecinco.TV`), and a scrape
of its list created 131 TV channels named like that. When the guide gives
nothing better than its id, the stream's own name is used. A name set later
in the interface always wins: an import never renames an existing channel.
"""
from app.models.models import EPGChannel, EPGSource, TVChannel
from app.services.epg_service import EPGService
from app.services.m3u_service import M3UService
from app.services.tvchannel_service import TVChannelService


def _guide(db, channels):
    source = EPGSource(url='https://example.com/epg.xml', name='Guide', enabled=True)
    db.add(source)
    db.flush()
    for xml_id, name in channels:
        db.add(EPGChannel(epg_source_id=source.id, channel_xml_id=xml_id, name=name))
    db.commit()


def _import(db, playlist):
    # As the scraper does since develop fbb5c1a: parse, then create TV channels
    # in a separate step (after import exclusions), staged in the caller's commit.
    service = M3UService()
    parsed = service.extract_channels_from_content(playlist)
    service.auto_create_tv_channels_from_epg(db, parsed, EPGService(db), TVChannelService(db))
    db.commit()
    return {tv.epg_id: tv.name for tv in db.query(TVChannel).all()}


LIST = """#EXTM3U
#EXTINF:-1 tvg-id="Telecinco.TV" group-title="Generalistas",Telecinco
https://cdn.example.com/t5.m3u8
#EXTINF:-1 tvg-id="Antena 3 HD",Antena 3 FHD
https://cdn.example.com/a3.m3u8
#EXTINF:-1 tvg-id="MLaLiga.TV",M+ LALIGA FHD --> NEW ERA II
acestream://{ace}
""".format(ace='1' * 40)


def test_SI_LA_GUIA_SOLO_DA_SU_ID_SE_USA_EL_NOMBRE_DE_LA_EMISION(db_session):
    _guide(db_session, [('Telecinco.TV', 'Telecinco.TV')])
    assert _import(db_session, LIST)['Telecinco.TV'] == 'Telecinco'


def test_UN_NOMBRE_DE_GUIA_DE_VERDAD_SE_RESPETA(db_session):
    """dobleM's `Antena 3 HD` is named `Antena 3`: that one is kept."""
    _guide(db_session, [('Antena 3 HD', 'Antena 3')])
    assert _import(db_session, LIST)['Antena 3 HD'] == 'Antena 3'


def test_LO_QUE_EL_PUBLICADOR_PONE_TRAS_LA_FLECHA_NO_ES_EL_NOMBRE(db_session):
    _guide(db_session, [('MLaLiga.TV', 'MLaLiga.TV')])
    assert _import(db_session, LIST)['MLaLiga.TV'] == 'M+ LALIGA FHD'


def test_UN_NOMBRE_PUESTO_A_MANO_GANA_AL_VOLVER_A_IMPORTAR(db_session):
    _guide(db_session, [('Telecinco.TV', 'Telecinco.TV')])
    _import(db_session, LIST)
    tv = db_session.query(TVChannel).filter(TVChannel.epg_id == 'Telecinco.TV').one()
    tv.name = 'Tele 5'
    db_session.commit()
    names = _import(db_session, LIST)
    assert names['Telecinco.TV'] == 'Tele 5'
    assert db_session.query(TVChannel).filter(TVChannel.epg_id == 'Telecinco.TV').count() == 1
