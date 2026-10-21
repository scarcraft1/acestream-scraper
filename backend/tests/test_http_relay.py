"""The TV relay opens official HTTP sources too.

A TV channel's playlist entry is the relay, and the relay only opened
acestreams: La 1 from RTVE or Antena 3 from Atresplayer, assigned to their
channel, were never tried. ffmpeg now copies them into the relay's MPEG-TS.
"""
import asyncio
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

def test_ffmpeg_only_reads_our_live_route_over_http(monkeypatch):
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
async def test_whole_ts_packets_are_sent_and_counted(tmp_path, monkeypatch):
    # 1000 bytes: five whole 188-byte packets, and 60 left over that never go out.
    monkeypatch.setattr(http_relay, 'ffmpeg_binary', lambda: _fake_ffmpeg(tmp_path, 'head -c 1000 /dev/zero'))
    claim = Mock(bytes_sent=0)
    chunks = [chunk async for chunk in http_relay.relay_http_stream('url:x', claim=claim)]
    assert sum(map(len, chunks)) == 940 and all(len(c) % 188 == 0 for c in chunks)
    assert claim.bytes_sent == 940


@pytest.mark.asyncio
async def test_an_ffmpeg_failure_is_a_source_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(http_relay, 'ffmpeg_binary', lambda: _fake_ffmpeg(tmp_path, 'exit 1'))
    with pytest.raises(EngineStreamError):
        await anext(http_relay.relay_http_stream('url:x'))


@pytest.mark.asyncio
async def test_closing_leaves_no_ffmpeg_running(tmp_path, monkeypatch):
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
async def test_the_relay_sends_http_sources_to_ffmpeg_and_acestreams_to_the_engine(monkeypatch):
    import app.services.tuner_playback_service as playback
    from app.services.stream_relay import RelayRegistry
    opened = []

    async def engine_relay(_engine, content_id, *args, **kwargs):
        opened.append(('engine', content_id))
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
    assert opened == [('http', 'url:la1'), ('engine', 'a' * 40)]


# --- which sources -----------------------------------------------------------

def test_the_official_source_goes_first_in_its_group_and_one_without_url_stays_out(db_session):
    tv = TVChannel(name='La 1', is_active=True)
    db_session.add(tv)
    db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add_all([
        AcestreamChannel(id='a' * 40, name='La 1 ace', tv_channel_id=tv.id, is_active=True, is_online=True,
                         last_checked=now, bitrate_bps=9_000_000),
        AcestreamChannel(id='url:' + '1' * 40, name='La 1 RTVE', tv_channel_id=tv.id, is_active=True, is_online=True,
                         last_checked=now, stream_url='https://rtve/la1.m3u8'),
        AcestreamChannel(id='url:' + '2' * 40, name='No URL', tv_channel_id=tv.id, is_active=True, is_online=True,
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
    """Stands in for bounded_source.source_get: answers from a table."""

    def __init__(self, table, delay=0.0):
        self.table, self.asked, self.delay = table, [], delay

    async def __call__(self, url, *, timeout=None, headers=None, max_bytes=None, sample=False, media_limit=None):
        self.asked.append((url, dict(headers or {})))
        if self.delay:
            await asyncio.sleep(self.delay)
        from app.utils.bounded_source import SourceResponse
        body = self.table.get(url)
        return SourceResponse(200 if body is not None else 404, body or b'', {'Content-Type': 'video/mp2t'})


@pytest.fixture
def lan(monkeypatch):
    from app.config.settings import get_settings
    from app.services.tuner_network import get_tuner_gate
    monkeypatch.setenv("TUNER_ALLOWED_NETWORKS", "*")
    get_settings.cache_clear(); get_tuner_gate.cache_clear()
    yield
    get_settings.cache_clear(); get_tuner_gate.cache_clear()


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
    monkeypatch.setattr(live, '_relay_cache', __import__('collections').OrderedDict())
    monkeypatch.setattr(live, '_relay_fetching', {})
    return client, origin, live


def _links(text):
    import re
    from urllib.parse import parse_qs, urlsplit
    found = re.findall(r'URI="([^"]+)"', text) + [l for l in text.splitlines() if l and not l.startswith('#')]
    return [parse_qs(urlsplit(link).query) for link in found]


def test_the_master_comes_back_through_us_without_subtitles(relay_live):
    """RTVE's WebVTT rendition made the minimal ffmpeg fail the whole opening."""
    client, origin, _ = relay_live
    text = client.get('/live/url%3A' + 'f' * 40 + '?r=1').text
    assert 'SUBTITLES' not in text
    assert sorted(q['u'][0] for q in _links(text)) == ['https://rtve/tdp/audio/es.m3u8', 'https://rtve/tdp/hd/index.m3u8']
    assert all(q['r'] == ['1'] for q in _links(text))
    assert origin.asked[0] == ('https://rtve/tdp/main.m3u8', {'Referer': 'https://rtve.es/'})


def test_the_segment_list_and_its_key_too(relay_live):
    from app.services import hls_manifest
    client, _, _ = relay_live
    url = 'https://rtve/tdp/hd/index.m3u8'
    text = client.get('/live/url%3A' + 'f' * 40, params={'r': '1', 'u': url, 's': hls_manifest.sign(url)}).text
    assert [q['u'][0] for q in _links(text)] == ['https://keys/k1', 'https://rtve/tdp/hd/seg_7.ts', 'https://rtve/tdp/hd/seg_8.ts']


def test_a_segment_is_passed_through_with_the_stream_headers(relay_live):
    from app.services import hls_manifest
    client, origin, _ = relay_live
    url = 'https://rtve/tdp/hd/seg_8.ts'
    response = client.get('/live/url%3A' + 'f' * 40, params={'r': '1', 'u': url, 's': hls_manifest.sign(url)})
    assert response.status_code == 200 and response.content == b'G' * 376
    assert origin.asked[-1] == (url, {'Referer': 'https://rtve.es/'})


def test_nothing_is_fetched_without_a_signature(relay_live):
    client, origin, _ = relay_live
    response = client.get('/live/url%3A' + 'f' * 40, params={'r': '1', 'u': 'http://192.168.1.1/admin', 's': 'x'})
    assert response.status_code == 403 and origin.asked == []


def test_only_from_this_server(relay_live, monkeypatch):
    """Not a proxy for the LAN: only the relay's own ffmpeg, on loopback."""
    client, origin, live = relay_live
    monkeypatch.setattr(live, '_is_loopback', lambda request: False)
    assert client.get('/live/url%3A' + 'f' * 40 + '?r=1').status_code == 403
    assert origin.asked == []


def test_without_r_kodi_gets_what_it_got_before(relay_live):
    """Kodi's own /live/ (manifests only, segments straight to the CDN) is unchanged."""
    from app.services import hls_manifest
    client, _, _ = relay_live
    url = 'https://rtve/tdp/hd/index.m3u8'
    text = client.get('/live/url%3A' + 'f' * 40, params={'u': url, 's': hls_manifest.sign(url)}).text
    assert 'https://rtve/tdp/hd/seg_8.ts' in text


# --- the short cache: Kodi's two GETs ask for the same things -----------------

@pytest.fixture
def cache(monkeypatch):
    from app.api.endpoints import live
    monkeypatch.setattr(live, '_relay_cache', __import__('collections').OrderedDict())
    monkeypatch.setattr(live, '_relay_fetching', {})
    origin = Origin({'https://cdn/seg_1.ts': b'G' * 1000, 'https://cdn/index.m3u8': MEDIA})
    monkeypatch.setattr(live, 'source_get', origin)
    return live, origin


def test_a_segment_is_fetched_once_for_both_gets(cache):
    live, origin = cache
    assert asyncio.run(live._relay_fetch('https://cdn/seg_1.ts', {}))[1] == b'G' * 1000
    assert asyncio.run(live._relay_fetch('https://cdn/seg_1.ts', {}))[1] == b'G' * 1000
    assert [url for url, _ in origin.asked] == ['https://cdn/seg_1.ts']


def test_a_playlist_expires_quickly(cache, monkeypatch):
    live, origin = cache
    clock = [100.0]
    monkeypatch.setattr(live.time, 'monotonic', lambda: clock[0])
    asyncio.run(live._relay_fetch('https://cdn/index.m3u8', {}))
    clock[0] += 1
    asyncio.run(live._relay_fetch('https://cdn/index.m3u8', {}))
    clock[0] += 2
    asyncio.run(live._relay_fetch('https://cdn/index.m3u8', {}))
    assert len(origin.asked) == 2          # reused within 2 s, fetched again after


def test_a_fetch_in_flight_is_waited_for(cache):
    live, origin = cache
    origin.delay = 0.2

    async def both():
        return await asyncio.gather(live._relay_fetch('https://cdn/seg_1.ts', {}),
                                    live._relay_fetch('https://cdn/seg_1.ts', {}))
    results = asyncio.run(both())
    assert [found[1] for found in results] == [b'G' * 1000] * 2
    assert len(origin.asked) == 1


def test_the_cache_is_bounded(cache, monkeypatch):
    live, origin = cache
    monkeypatch.setattr(live, 'RELAY_CACHE_MAX_BYTES', 1500)
    origin.table['https://cdn/seg_2.ts'] = b'G' * 1000
    asyncio.run(live._relay_fetch('https://cdn/seg_1.ts', {}))
    asyncio.run(live._relay_fetch('https://cdn/seg_2.ts', {}))
    assert list(live._relay_cache) == ['https://cdn/seg_2.ts']


def test_a_failure_is_not_cached(cache):
    live, origin = cache
    for _ in range(2):
        with pytest.raises(ValueError):
            asyncio.run(live._relay_fetch('https://cdn/missing.ts', {}))
    assert len(origin.asked) == 2


def test_the_relay_uses_the_providers_current_headers_not_the_stored_ones(lan, client, db_session, monkeypatch):
    """7 TV La Rioja's playback headers changed and a stored Referer went stale:
    for a broadcaster, its current PLAY_HEADERS win."""
    from app.api.endpoints import live
    from app.services.live_providers import sietetv
    db_session.add(AcestreamChannel(id='url:' + 'e' * 40, name='7 TV', stream_url='7riojaplay://x',
                                    stream_headers={'Referer': 'https://old.example/'}))
    db_session.commit()
    origin = Origin({'https://cdndirector/live/x.m3u8?sec=1': MEDIA})
    monkeypatch.setattr(live, 'source_get', origin)
    monkeypatch.setattr(live, '_is_loopback', lambda request: True)
    monkeypatch.setattr(live, '_relay_cache', __import__('collections').OrderedDict())
    monkeypatch.setattr(live, '_relay_fetching', {})

    async def resolved(_stream_url):
        return 'https://cdndirector/live/x.m3u8?sec=1'
    monkeypatch.setattr(live, '_resolve_once', resolved)
    assert client.get('/live/url%3A' + 'e' * 40 + '?r=1').status_code == 200
    sent = origin.asked[0][1]
    assert sent['User-Agent'] == sietetv.PLAY_HEADERS['User-Agent']
    assert sent.get('Referer') == sietetv.PLAY_HEADERS.get('Referer')
