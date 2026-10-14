"""IPTV streams are emitted with their own URL, not base_url + id.

Every base URL pattern assumes an engine that resolves an acestream id. A
stream stored with `stream_url` (see test_iptv_streams.py) has an opaque
`url:<sha1>` id; rendered through the pattern it became
`http://host:6878/ace/getstream?id=url:<sha1>`, which no engine can play.
"""

import uuid

from app.models.models import AcestreamChannel
from app.services.playlist_service import PlaylistService

ACE = "1" * 40
IPTV_URL = "https://cdn.example.com/la1/master.m3u8"
IPTV_ID = "url:" + "a" * 40


class TestStreamLink:
    def test_LA_URL_PROPIA_GANA_AL_PREFIJO(self):
        link = PlaylistService._stream_link(
            "http://h:6878/ace/getstream?id=", IPTV_ID, stream_url=IPTV_URL)
        assert link == IPTV_URL

    def test_GANA_TAMBIEN_A_LA_MASCARA_Y_SIN_PID(self):
        """Un pid pegado a una URL de un CDN no significa nada y puede
        romperla."""
        link = PlaylistService._stream_link(
            "http://h/ace/getstream?id={channel_id}&pid={pid}", IPTV_ID,
            pid=7, stream_url=IPTV_URL)
        assert link == IPTV_URL

    def test_GANA_AL_RELEVO_POR_CANAL_DE_TV(self):
        link = PlaylistService._stream_link(
            "http://relay/tv/{tv_channel_id}", IPTV_ID, tv_channel_id=3,
            stream_url=IPTV_URL)
        assert link == IPTV_URL

    def test_SIN_URL_PROPIA_TODO_SIGUE_IGUAL(self):
        """Guarda: el acestream no cambia. Pasa con y sin el arreglo."""
        assert PlaylistService._stream_link(
            "http://h:6878/ace/getstream?id=", ACE) == \
            "http://h:6878/ace/getstream?id=" + ACE


def _canal(db_session, cid, name, stream_url=None, tv_channel_id=None):
    c = AcestreamChannel(
        id=cid, name=name, is_active=True, is_online=True,
        source_url="http://lista.example/" + uuid.uuid4().hex,
        stream_url=stream_url, tv_channel_id=tv_channel_id)
    db_session.add(c)
    db_session.commit()
    return c


class TestPlaylistOutput:
    def test_LA_LISTA_PUBLICA_LA_URL_DEL_IPTV_Y_EL_ID_DEL_ACESTREAM(
            self, client, db_session):
        _canal(db_session, ACE, "Canal acestream")
        _canal(db_session, IPTV_ID, "La 1", stream_url=IPTV_URL)
        base = "http://h:6878/ace/getstream?id="
        r = client.get("/playlist.m3u", params={
            "base_url": base, "only_online": "false"})
        assert r.status_code == 200, r.text
        lineas = [l for l in r.text.splitlines() if l and not l.startswith("#")]
        assert IPTV_URL in lineas, lineas
        assert base + ACE in lineas, lineas
        assert not any("url:" in l for l in lineas), lineas

    def test_LA_LISTA_DE_CANALES_DE_TV_TAMBIEN(
            self, client, db_session, seed_tv_channels):
        tv = seed_tv_channels[0]
        _canal(db_session, IPTV_ID, "La 1", stream_url=IPTV_URL,
               tv_channel_id=tv.id)
        r = client.get("/api/v1/playlists/tv-channels/m3u")
        assert r.status_code == 200, r.text
        assert IPTV_URL in r.text, r.text
        assert "url:" not in r.text, r.text
