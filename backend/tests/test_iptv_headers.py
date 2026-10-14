"""Headers a playlist publishes for an IPTV stream reach the player.

Some CDNs answer 403 without a Referer or a User-Agent. On a public TDT list
30 of 576 entries publish `#EXTVLCOPT:http-referrer`, and their domains were
exactly the ones answering 403: the "blocked" channels were only missing the
header the list itself was publishing.

Kodi reads `url|Name=value&...` and sends those headers to the playlist and
to every segment. Values must be percent-encoded: unescaped, Kodi cuts each
at its first space and a 120-character User-Agent arrives as `Mozilla/5.0`.
"""
import uuid

from app.models.models import AcestreamChannel
from app.services.m3u_service import M3UService
from app.services.playlist_service import PlaylistService

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/135.0.0.0 Safari/537.36"
REF = "https://player.instantvideocloud.net/"
URL = "https://liveingesta318.cdnmedia.tv/101weblive/antequera/playlist.m3u8"

LISTA = f"""#EXTM3U
#EXTINF:-1 tvg-id="101TV.Antequera" group-title="Andalucía",101TV Antequera
#EXTVLCOPT:http-referrer={REF}
#EXTVLCOPT:http-user-agent={UA}
{URL}
#EXTINF:-1,Sin cabeceras
https://cdn.example.com/otro.m3u8
#EXTINF:-1,Un acestream
#EXTVLCOPT:http-referrer=https://no.importa/
acestream://{'1' * 40}
"""


def _lee():
    return {c[1]: c[2] for c in M3UService().extract_channels_from_content(LISTA)}


def test_SE_LEEN_LAS_CABECERAS_DE_SU_ENTRADA():
    m = _lee()["101TV Antequera"]
    assert m.get("stream_headers") == {"Referer": REF, "User-Agent": UA}, m


def test_NO_SE_ARRASTRAN_A_LA_SIGUIENTE():
    """Cada #EXTINF empieza de cero: las de una no son de la otra."""
    m = _lee()
    # Sin esto pasaria tambien si no se leyera NINGUNA cabecera.
    assert m["101TV Antequera"].get("stream_headers"), m
    assert "stream_headers" not in m["Sin cabeceras"]


def test_UN_ACESTREAM_NO_LAS_LLEVA():
    """Van por el motor, no por HTTP: no significan nada."""
    m = _lee()
    assert m["101TV Antequera"].get("stream_headers"), m
    assert "stream_headers" not in m["Un acestream"]


def test_SE_EMITEN_ESCAPADAS_Y_EN_ORDEN():
    link = PlaylistService._stream_link(
        "acestream://", "url:x", stream_url=URL,
        stream_headers={"Referer": REF, "User-Agent": UA})
    url, _, tubo = link.partition("|")
    assert url == URL
    assert tubo.startswith("User-Agent=Mozilla%2F5.0%20%28"), tubo
    assert "&Referer=https%3A%2F%2Fplayer.instantvideocloud.net%2F" in tubo, tubo


def test_NINGUN_ESPACIO_LLEGA_A_KODI():
    """El fallo del 01-10: Kodi corta en el primer espacio sin escapar."""
    link = PlaylistService._stream_link(
        "x", "url:x", stream_url=URL, stream_headers={"User-Agent": UA})
    assert "|User-Agent=" in link, link      # que de verdad las lleve
    assert " " not in link, link


def test_SI_LA_URL_YA_TRAE_TUBO_SE_SUMAN():
    link = PlaylistService._stream_link(
        "x", "url:x", stream_url=URL + "|Origin=https%3A%2F%2Fa",
        stream_headers={"Referer": REF})
    assert link.count("|") == 1, link
    assert link.endswith("&Referer=https%3A%2F%2Fplayer.instantvideocloud.net%2F"), link


def test_SIN_CABECERAS_LA_URL_SALE_TAL_CUAL():
    """Guarda: lo que funcionaba sigue igual."""
    assert PlaylistService._stream_link("x", "url:x", stream_url=URL) == URL


def test_LA_LISTA_LAS_PUBLICA(client, db_session):
    db_session.add(AcestreamChannel(
        id="url:" + "b" * 40, name="101TV Antequera", is_active=True,
        is_online=True, source_url="http://l/" + uuid.uuid4().hex,
        stream_url=URL, stream_headers={"Referer": REF}))
    db_session.commit()
    r = client.get("/playlist.m3u", params={"only_online": "false"})
    assert r.status_code == 200, r.text
    assert URL + "|Referer=https%3A%2F%2Fplayer.instantvideocloud.net%2F" in r.text, r.text


# A stream without headers stored the JSON text 'null', which SQL counts as
# NOT NULL: on the fork's test database `stream_headers IS NOT NULL` said 576
# of 576 IPTV streams had headers when 36 did.

def test_SIN_CABECERAS_SE_GUARDA_NULL_DE_VERDAD(db_session):
    from sqlalchemy import text
    db_session.add(AcestreamChannel(id=f"url:{uuid.uuid4().hex}", name="Sin",
                                    stream_url=URL, stream_headers=None))
    db_session.commit()
    assert db_session.execute(text(
        "SELECT count(*) FROM acestream_channels WHERE stream_headers IS NOT NULL"
    )).scalar_one() == 0


def test_LA_MIGRACION_LIMPIA_LOS_NULL_DE_TEXTO(tmp_path):
    from sqlalchemy import create_engine, text
    from migration_test_utils import database_url_for, upgrade_to_revision

    db_path = tmp_path / 'null-headers.db'
    upgrade_to_revision(db_path, '20261014_1400')
    engine = create_engine(database_url_for(db_path))
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO acestream_channels (id, name, stream_headers) VALUES "
                "('a', 'Sin', 'null'), ('b', 'Con', '{\"Referer\": \"https://x/\"}')"
            ))
        upgrade_to_revision(db_path, '20261014_1500')
        with engine.connect() as connection:
            rows = dict(connection.execute(text(
                "SELECT id, stream_headers FROM acestream_channels"
            )).all())
        assert rows == {'a': None, 'b': '{"Referer": "https://x/"}'}
    finally:
        engine.dispose()
