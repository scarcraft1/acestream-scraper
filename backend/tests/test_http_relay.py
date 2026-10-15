"""The TV relay opens official HTTP sources too.

A TV channel's playlist entry is the relay, and the relay only opened
acestreams: La 1 from RTVE or Antena 3 from Atresplayer, assigned to their
channel, were never tried. ffmpeg now copies them into the relay's MPEG-TS.
"""
import os
import stat
import time
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.models.models import AcestreamChannel, TVChannel
from app.services import http_relay
from app.services.stream_relay import EngineStreamError
from app.services.tuner_service import TunerService


# --- what ffmpeg is asked ----------------------------------------------------

def test_FFMPEG_SOLO_LEE_DE_NUESTRA_LIVE_POR_HTTP(monkeypatch):
    """The upstream image's ffmpeg has no TLS (measured: "Protocol not found"
    on https): it reads our loopback /live/ and copies, never transcodes."""
    monkeypatch.setenv('FLASK_PORT', '8000')
    source = http_relay.relay_input('url:abc')
    assert source == 'http://127.0.0.1:8000/live/url%3Aabc?r=1'
    argv = http_relay.http_relay_argv('/ff', source)
    assert argv[argv.index('-protocol_whitelist') + 1] == 'http,tcp,crypto'
    assert argv[argv.index('-i') + 1] == source
    # Our URLs end in a query: without this every segment is dropped.
    assert argv[argv.index('-extension_picky') + 1] == '0'
    assert argv[argv.index('-c') + 1] == 'copy' and argv[-1] == 'pipe:1'


# --- the relay ---------------------------------------------------------------

def _fake_ffmpeg(tmp_path, body):
    path = tmp_path / 'ffmpeg'
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.mark.asyncio
async def test_DA_PAQUETES_TS_ENTEROS_Y_LOS_CUENTA(tmp_path, monkeypatch):
    # 1000 bytes: five whole 188-byte packets, and 60 left over that never go out.
    monkeypatch.setattr(http_relay, 'ffmpeg_binary', lambda: _fake_ffmpeg(tmp_path, 'head -c 1000 /dev/zero'))
    claim = Mock(bytes_sent=0)
    chunks = [chunk async for chunk in http_relay.relay_http_stream('url:x', claim=claim)]
    assert sum(map(len, chunks)) == 940 and all(len(c) % 188 == 0 for c in chunks)
    assert claim.bytes_sent == 940


@pytest.mark.asyncio
async def test_SI_FFMPEG_FALLA_ES_UN_FALLO_DE_FUENTE(tmp_path, monkeypatch):
    monkeypatch.setattr(http_relay, 'ffmpeg_binary', lambda: _fake_ffmpeg(tmp_path, 'exit 1'))
    with pytest.raises(EngineStreamError):
        await anext(http_relay.relay_http_stream('url:x'))


@pytest.mark.asyncio
async def test_AL_CERRAR_NO_QUEDA_FFMPEG_VIVO(tmp_path, monkeypatch):
    pidfile = tmp_path / 'pid'
    monkeypatch.setattr(http_relay, 'ffmpeg_binary', lambda: _fake_ffmpeg(
        tmp_path, f'echo $$ > {pidfile}; head -c 376 /dev/zero; exec sleep 30'))
    stream = http_relay.relay_http_stream('url:x')
    assert len(await anext(stream)) == 376
    started = time.monotonic()
    await stream.aclose()
    # At once: the 2 s SIGKILL fallback would be 2 s more on every zap.
    assert time.monotonic() - started < 1
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_EL_RELE_MANDA_LAS_HTTP_AL_PUENTE_Y_LAS_ACESTREAM_AL_MOTOR(monkeypatch):
    import app.services.tuner_playback_service as playback
    from app.services.stream_relay import RelayRegistry
    opened = []

    async def engine_relay(_engine, content_id, *args, **kwargs):
        opened.append(('motor', content_id))
        yield b'ace'

    async def http(content_id, **kwargs):
        opened.append(('http', content_id))
        raise EngineStreamError('down')
        yield b''
    monkeypatch.setattr(playback, 'relay_engine_stream', engine_relay)
    monkeypatch.setattr(playback, 'relay_http_stream', http)
    registry = RelayRegistry()
    claim = registry.try_open('url:la1', 'viewer', 1)
    stream = playback.relay_ranked_streams(Mock(), ['url:la1', 'a' * 40], 'viewer', claim, registry=registry,
                                           attempt_seconds=1, startup_budget_seconds=2)
    assert await anext(stream) == b'ace'
    await stream.aclose()
    assert opened == [('http', 'url:la1'), ('motor', 'a' * 40)]


# --- which sources -----------------------------------------------------------

def test_LA_OFICIAL_VA_PRIMERO_EN_SU_GRUPO_Y_SIN_URL_NO_ENTRA(db_session):
    tv = TVChannel(name='La 1', is_active=True)
    db_session.add(tv)
    db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add_all([
        AcestreamChannel(id='a' * 40, name='La 1 ace', tv_channel_id=tv.id, is_active=True, is_online=True,
                         last_checked=now, bitrate_bps=9_000_000),
        AcestreamChannel(id='url:' + '1' * 40, name='La 1 RTVE', tv_channel_id=tv.id, is_active=True, is_online=True,
                         last_checked=now, stream_url='https://rtve/la1.m3u8'),
        AcestreamChannel(id='url:' + '2' * 40, name='Sin URL', tv_channel_id=tv.id, is_active=True, is_online=True,
                         last_checked=now),
    ])
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == ['url:' + '1' * 40, 'a' * 40]


# --- /live/{id}?r=1: what the relay's ffmpeg is served ------------------------

MASTER = (b'#EXTM3U\n'
          b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="es",URI="audio/es.m3u8"\n'
          b'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",NAME="es",URI="subs/es.m3u8"\n'
          b'#EXT-X-STREAM-INF:BANDWIDTH=5000000,AUDIO="a",SUBTITLES="s"\nhd/index.m3u8\n')
MEDIA = (b'#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n'
         b'#EXT-X-KEY:METHOD=AES-128,URI="https://keys/k1"\n'
         b'#EXTINF:5,\nseg_7.ts\n#EXTINF:5,\nseg_8.ts\n')


class Origin:
    def __init__(self, table):
        self.table, self.asked = table, []

    def __call__(self, url, timeout=None, headers=None):
        self.asked.append((url, dict(headers or {})))
        body = self.table.get(url)

        class R:
            status_code = 200 if body is not None else 404
            content = body or b''
            headers = {'Content-Type': 'video/mp2t'}

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f'HTTP {self.status_code}')
        return R()


@pytest.fixture
def relay_live(lan, client, db_session, monkeypatch):
    from app.api.endpoints import live
    db_session.add(AcestreamChannel(id='url:' + 'f' * 40, name='Teledeporte',
                                    stream_url='https://rtve/tdp/main.m3u8', stream_headers={'Referer': 'https://rtve.es/'}))
    db_session.commit()
    origin = Origin({'https://rtve/tdp/main.m3u8': MASTER, 'https://rtve/tdp/hd/index.m3u8': MEDIA,
                     'https://rtve/tdp/hd/seg_8.ts': b'G' * 376})
    monkeypatch.setattr(live, 'source_get', origin)
    monkeypatch.setattr(live, '_is_loopback', lambda request: True)
    return client, origin, live


def _links(text):
    import re
    from urllib.parse import parse_qs, urlsplit
    found = re.findall(r'URI="([^"]+)"', text) + [l for l in text.splitlines() if l and not l.startswith('#')]
    return [parse_qs(urlsplit(link).query) for link in found]


@pytest.fixture
def lan(monkeypatch):
    from app.config.settings import get_settings
    from app.services.tuner_network import get_tuner_gate
    monkeypatch.setenv("TUNER_ALLOWED_NETWORKS", "*")
    get_settings.cache_clear(); get_tuner_gate.cache_clear()
    yield
    get_settings.cache_clear(); get_tuner_gate.cache_clear()


def test_EL_MAESTRO_VUELVE_POR_NOSOTROS_SIN_SUBTITULOS(relay_live):
    """RTVE's WebVTT rendition made the minimal ffmpeg fail the whole opening."""
    client, origin, _ = relay_live
    text = client.get('/live/url%3A' + 'f' * 40 + '?r=1').text
    assert 'SUBTITLES' not in text
    assert sorted(q['u'][0] for q in _links(text)) == ['https://rtve/tdp/audio/es.m3u8', 'https://rtve/tdp/hd/index.m3u8']
    assert all(q['r'] == ['1'] for q in _links(text))
    assert origin.asked[0] == ('https://rtve/tdp/main.m3u8', {'Referer': 'https://rtve.es/'})


def test_LA_LISTA_DE_SEGMENTOS_Y_SU_CLAVE_TAMBIEN(relay_live):
    from app.services import hls_manifest
    client, _, _ = relay_live
    url = 'https://rtve/tdp/hd/index.m3u8'
    text = client.get('/live/url%3A' + 'f' * 40, params={'r': '1', 'u': url, 's': hls_manifest.sign(url)}).text
    assert [q['u'][0] for q in _links(text)] == ['https://keys/k1', 'https://rtve/tdp/hd/seg_7.ts', 'https://rtve/tdp/hd/seg_8.ts']


def test_UN_SEGMENTO_SE_PASA_TAL_CUAL_CON_SUS_CABECERAS(relay_live):
    from app.services import hls_manifest
    client, origin, _ = relay_live
    url = 'https://rtve/tdp/hd/seg_8.ts'
    response = client.get('/live/url%3A' + 'f' * 40, params={'r': '1', 'u': url, 's': hls_manifest.sign(url)})
    assert response.status_code == 200 and response.content == b'G' * 376
    assert origin.asked[-1] == (url, {'Referer': 'https://rtve.es/'})


def test_SIN_FIRMA_NO_SE_DESCARGA_NADA(relay_live):
    client, origin, _ = relay_live
    response = client.get('/live/url%3A' + 'f' * 40, params={'r': '1', 'u': 'http://192.168.1.1/admin', 's': 'x'})
    assert response.status_code == 403 and origin.asked == []


def test_SOLO_DESDE_ESTE_SERVIDOR(relay_live, monkeypatch):
    """Not a proxy for the LAN: only the relay's own ffmpeg, on loopback."""
    client, origin, live = relay_live
    monkeypatch.setattr(live, '_is_loopback', lambda request: False)
    assert client.get('/live/url%3A' + 'f' * 40 + '?r=1').status_code == 403
    assert origin.asked == []


def test_SIN_R_KODI_SIGUE_COMO_ANTES(relay_live):
    """Kodi's own /live/ (manifests only, segments straight to the CDN) is unchanged."""
    client, _, _ = relay_live
    text = client.get('/live/url%3A' + 'f' * 40 + '?u=https%3A%2F%2Frtve%2Ftdp%2Fhd%2Findex.m3u8&s='
                      + __import__('app.services.hls_manifest', fromlist=['sign']).sign('https://rtve/tdp/hd/index.m3u8')).text
    assert 'https://rtve/tdp/hd/seg_8.ts' in text
