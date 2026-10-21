"""TV relay for HTTP sources (`url:<sha1>`): broadcasters and IPTV lists.

A TV channel's playlist entry is the relay (`/tuner/channel/{id}.ts`), so an
official source assigned to the channel -- La 1 from RTVE, Antena 3 from
Atresplayer -- was never tried: the relay only opened acestreams. Here ffmpeg
reads the HLS stream and copies it, without transcoding, into the MPEG-TS
the relay sends; Kodi receives it like any other source of the channel.

ffmpeg always opens our own `/live/{id}?r=1` on loopback. The upstream
image's ffmpeg is a minimal build WITHOUT TLS (measured: "Protocol not found"
on https), so that route fetches every manifest, key and segment from the
origin with the stream's headers through the guarded client, resolves a
broadcaster (`mediaset://T5`) first, and hands them to ffmpeg over plain
HTTP (app.api.endpoints.live, hls_manifest.serve_all). Only loopback HTTP
is allowed to ffmpeg: a list cannot make it read a file or reach the LAN.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from typing import AsyncIterator, Optional
from urllib.parse import quote

import anyio

from app.services.stream_relay import EngineStreamError, RelayInfo
from app.services.tuner_remux import ffmpeg_binary

# ffmpeg gives up on a read after this long (it is in microseconds there):
# the relay's own stall detection then moves to the next source.
READ_TIMEOUT_SECONDS = 15
PROTOCOLS = 'http,tcp,crypto'


def is_http_source(content_id: str) -> bool:
    return content_id.startswith('url:')


def relay_input(stream_id: str) -> str:
    port = os.environ.get('FLASK_PORT') or '8000'
    return f"http://127.0.0.1:{port}/live/{quote(stream_id, safe='')}?r=1"


def http_relay_argv(binary: str, source: str) -> list[str]:
    return [binary, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-protocol_whitelist', PROTOCOLS, '-rw_timeout', str(READ_TIMEOUT_SECONDS * 1_000_000),
            # Our loopback URLs end in a query, not in .ts: without this the
            # hls demuxer drops every segment (allowed_segment_extensions).
            '-extension_picky', '0',
            '-i', source,
            # One video and one audio, as ffmpeg picks them: the best variant.
            # Subtitles and data cannot be copied into MPEG-TS from HLS.
            '-sn', '-dn', '-c', 'copy',
            '-f', 'mpegts', '-mpegts_flags', '+resend_headers', '-muxdelay', '0', '-flush_packets', '1', 'pipe:1']


async def relay_http_stream(stream_id: str, *, claim: Optional[RelayInfo] = None) -> AsyncIterator[bytes]:
    """Yield MPEG-TS bytes for an HTTP source; EngineStreamError when it
    cannot be opened or ffmpeg fails, like an engine source."""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *http_relay_argv(ffmpeg_binary(), relay_input(stream_id)),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        buffered = bytearray()
        while chunk := await process.stdout.read(64 * 1024):
            buffered.extend(chunk)
            complete = len(buffered) // 188 * 188
            if complete:
                if claim is not None:
                    claim.bytes_sent += complete
                yield bytes(buffered[:complete])
                del buffered[:complete]
        if await process.wait() != 0:
            raise EngineStreamError('The HTTP source could not be read')
    except OSError as exc:
        raise EngineStreamError('Unable to start the HTTP relay') from exc
    finally:
        with anyio.CancelScope(shield=True):
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
