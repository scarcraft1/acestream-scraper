"""Playlists whose entries are plain URLs, not acestream ids.

Until now `M3UService.extract_channels_from_m3u` dropped, in silence, every
line that did not carry an acestream id. Importing an ordinary IPTV playlist
produced zero channels and reported no error, which is indistinguishable from
an empty playlist.

Measured on a running box: 592 of 1.599 sources (37%) are plain HTTP, and they
reach 458 of 802 channels -- more than half the grid.
"""

import pytest
from app.services.m3u_service import M3UService


IPTV = """#EXTM3U
#EXTINF:-1 tvg-id="La1.es" group-title="TDT",La 1
https://cdn.example.com/la1/master.m3u8
#EXTINF:-1 tvg-id="La2.es" group-title="TDT",La 2
https://cdn.example.com/la2/master.m3u8
"""

MIXTA = """#EXTM3U
#EXTINF:-1 group-title="Deportes",Canal de acestream
acestream://1111111111111111111111111111111111111111
#EXTINF:-1 group-title="Deportes",Canal de IPTV
https://cdn.example.com/dep/master.m3u8
"""

BASURA = """#EXTM3U
#EXTINF:-1,Algo que no se puede abrir
rtmp://servidor/directo
#EXTINF:-1,Otra cosa
/ruta/relativa.ts
"""


@pytest.fixture
def servicio():
    return M3UService()


def extrae(servicio, texto):
    """`extract_channels_from_content` analiza; `extract_channels_from_m3u`
    DESCARGA y luego analiza. Aqui interesa lo primero."""
    return servicio.extract_channels_from_content(texto)


def test_UNA_LISTA_IPTV_YA_NO_DA_CERO(servicio):
    """El caso que motiva el cambio: antes devolvia [] sin decir nada."""
    canales = extrae(servicio, IPTV)
    assert len(canales) == 2, canales
    assert [c[1] for c in canales] == ['La 1', 'La 2']


def test_SE_GUARDA_LA_URL_PARA_PODER_SERVIRLA(servicio):
    canales = extrae(servicio, IPTV)
    urls = [c[2].get('stream_url') for c in canales]
    assert urls == ['https://cdn.example.com/la1/master.m3u8',
                    'https://cdn.example.com/la2/master.m3u8'], urls


def test_EL_ID_NO_SE_PUEDE_CONFUNDIR_CON_UN_ACESTREAM(servicio):
    """40 hex a secas es un content id o un infohash. Esto no lo es.

    Asi, todo el codigo que pregunta al motor por un id de 40 hex sigue
    ignorando estos flujos sin tocar una linea.
    """
    import re
    canales = extrae(servicio, IPTV)
    # SIN ESTO el test pasa recorriendo una lista vacia, o sea que pasaria
    # igual con el codigo de antes y no probaria nada.
    assert len(canales) == 2, canales
    for cid, _n, _m in canales:
        assert cid.startswith('url:'), cid
        assert not re.fullmatch(r'[a-fA-F0-9]{40}', cid), cid


def test_EL_ID_ES_ESTABLE_entre_rastreos(servicio):
    """Si cambiara, cada rastreo duplicaria la fila en vez de actualizarla."""
    a = extrae(servicio, IPTV)
    b = extrae(servicio, IPTV)
    # Lo mismo: dos listas vacias tambien son iguales.
    assert len(a) == 2, a
    assert [c[0] for c in a] == [c[0] for c in b]


def test_LOS_ACESTREAM_SIGUEN_IGUAL(servicio):
    """Que el acestream de una lista MIXTA siga entrando igual y sin URL.

    No es una guarda de marcha atras --tambien comprueba que el de IPTV
    entra--, asi que falla sin el arreglo. Se dice para no contarlo como
    cobertura de lo que no es.
    """
    canales = extrae(servicio, MIXTA)
    assert len(canales) == 2, canales
    ace = [c for c in canales if c[0] == '1' * 40]
    assert len(ace) == 1, canales
    assert ace[0][2].get('stream_url') is None, ace


def test_LAS_LINEAS_QUE_NADIE_PUEDE_ABRIR_SE_SIGUEN_TIRANDO(servicio):
    """rtmp y rutas relativas no son canales; dejarlas pasar seria convertir
    basura en filas del catalogo."""
    assert extrae(servicio, BASURA) == []


def test_SE_CONSERVAN_GRUPO_Y_TVG_ID(servicio):
    """Son lo que luego cuelga el canal de su guia."""
    _cid, _n, meta = extrae(servicio, IPTV)[0]
    assert meta.get('tvg_id') == 'La1.es', meta
    assert meta.get('group_title') == 'TDT', meta
