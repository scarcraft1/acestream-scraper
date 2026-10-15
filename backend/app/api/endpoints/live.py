"""`/live/{stream_id}`: open a stream that cannot be handed to the player as is.

Two kinds go through here:

- A broadcaster's live channel (`mediaset://T5`): resolved when opened and
  answered with a redirect to the freshly signed link.
- An HLS stream whose playlist publishes headers (101TV, PTV...): its
  manifests are fetched with those headers and served whole, because ffmpeg
  reads them cut short from the CDN and drops the list. See hls_manifest.

Mounted on the app, not on the API router: a player following a playlist
link cannot send the API token. Gated on TUNER_ALLOWED_NETWORKS instead,
like the tuner routes.

The playback headers travel in the playlist entry (`url|Referer=...`);
Kodi keeps sending them after a redirect and to every segment.
"""
import asyncio
import ipaddress
import threading
import time
from collections import OrderedDict
from urllib.parse import quote, urljoin

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.models import AcestreamChannel
from app.services import hls_manifest, live_providers
from app.services import viewing
from app.services.tuner_network import require_tuner_network
from app.utils.outbound_http import source_get

live_router = APIRouter(prefix="/live", dependencies=[Depends(require_tuner_network)], tags=["live"])

# Opening one channel is a burst: Kodi asks with HEAD for the type, then GETs
# twice. Each GET would resolve again -- three requests and about six seconds
# on Mediaset -- so a link is reused for a few seconds. Far below the ~5 minutes
# a Mediaset master stays valid for opening; a later reconnect resolves anew.
REUSE_SECONDS = 30
HLS_TYPE = "application/vnd.apple.mpegurl"
_recent: dict = {}
# Whole budget for answering one GET, resolution or manifest fetch included.
# Kodi waits on this request in its interface thread: a broadcaster that
# hangs (Mediaset down: three requests of up to 20 s each) left the remote
# dead for 20 s and a surprise dialog a minute later. Past the budget the
# player gets a 502 at once, which it handles cleanly (dialog in 2 s, remote
# responsive). Mediaset's slowest measured resolution was about 6 s.
# Raised from 8 to 15 s at Óscar's request: with the playlist's mimetype the
# budget no longer blocks the remote (measured 0 s), it only delays the error,
# so a slow broadcaster gets more room.
OPEN_BUDGET_SECONDS = 15
# A manifest is a few hundred KB at most (cdnmedia's DVR chunklist: 290 KB).
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
# A segment for the TV relay: a few seconds of 1080p is a few MB.
MAX_SEGMENT_BYTES = 64 * 1024 * 1024
MAX_REDIRECTS = 5


def _resolve_once(stream_url: str):
    now = time.monotonic()
    for key in [k for k, (at, _) in _recent.items() if now - at > REUSE_SECONDS]:
        del _recent[key]
    if stream_url in _recent:
        return _recent[stream_url][1]
    link = live_providers.resolve(stream_url)
    if link:
        _recent[stream_url] = (now, link)
    return link


def _fetch_manifest(url: str, headers: dict, limit: int = MAX_MANIFEST_BYTES):
    """(final URL, body, content type), following redirects through the
    guarded client: every hop's address is checked, these URLs come from
    third-party playlists."""
    for _ in range(MAX_REDIRECTS + 1):
        response = source_get(url, timeout=20, headers=headers)
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get('Location'):
            url = urljoin(url, response.headers['Location'])
            continue
        response.raise_for_status()
        if len(response.content) > limit:
            raise ValueError('response too large')
        return url, response.content, response.headers.get('Content-Type') or ''
    raise ValueError('too many redirects')


# Kodi opens a channel with two GETs: each starts the relay's ffmpeg, and the
# second asked the broadcaster again for the master and the first segments
# the first had just fetched (the other agent counted ~17 requests). What the
# relay fetched is kept briefly: a segment does not change, a playlist does
# with every new segment. Bounded, the box has little memory.
RELAY_CACHE_SEGMENT_SECONDS = 30
RELAY_CACHE_PLAYLIST_SECONDS = 2
RELAY_CACHE_MAX_BYTES = 48 * 1024 * 1024
_relay_cache: "OrderedDict[str, tuple]" = OrderedDict()
_relay_cache_lock = threading.Lock()
_relay_fetching: dict = {}


def _relay_fetch(url: str, headers: dict):
    """_fetch_manifest for the relay, through the short cache. A fetch of
    the same URL already in flight is waited for, not repeated: both
    ffmpegs ask for the same segments at nearly the same moment."""
    while True:
        with _relay_cache_lock:
            hit = _relay_cache.get(url)
            if hit is not None:
                expires, found = hit
                if time.monotonic() < expires:
                    _relay_cache.move_to_end(url)
                    return found
                del _relay_cache[url]
            running = _relay_fetching.get(url)
            if running is None:
                running = _relay_fetching[url] = threading.Event()
                break
        running.wait(OPEN_BUDGET_SECONDS)
    try:
        found = _fetch_manifest(url, headers, MAX_SEGMENT_BYTES)
        playlist = b'#EXTM3U' in found[1][:512]
        ttl = RELAY_CACHE_PLAYLIST_SECONDS if playlist else RELAY_CACHE_SEGMENT_SECONDS
        with _relay_cache_lock:
            _relay_cache[url] = (time.monotonic() + ttl, found)
            size = sum(len(entry[1][1]) for entry in _relay_cache.values())
            while size > RELAY_CACHE_MAX_BYTES and len(_relay_cache) > 1:
                _, (_, (_, body, _)) = _relay_cache.popitem(last=False)
                size -= len(body)
        return found
    finally:
        with _relay_cache_lock:
            _relay_fetching.pop(url, None)
        running.set()


def _serve_for_relay(stream_id: str, url: str, headers: dict):
    """Loopback only: what the TV relay's ffmpeg reads (see
    hls_manifest.serve_all). A manifest comes back rewritten; anything else
    -- a segment, a key, an init map -- is passed through as fetched."""
    try:
        final_url, body, content_type = _relay_fetch(url, headers)
    except Exception as error:
        return JSONResponse({"detail": f"The origin did not answer: {error}"}, status_code=502)
    if '#EXTM3U' not in body[:512].decode('utf-8', 'replace'):
        return Response(content=body, media_type=content_type or 'application/octet-stream')

    def link(absolute_url: str) -> str:
        return (f"/live/{quote(stream_id, safe='')}?r=1&u={quote(absolute_url, safe='')}"
                f"&s={hls_manifest.sign(absolute_url)}")

    served = hls_manifest.serve_all(body.decode('utf-8', 'replace'), final_url, link)
    return Response(content=served.encode('utf-8'), media_type=HLS_TYPE)


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ''
    try:
        return ipaddress.ip_address(host.strip('[]')).is_loopback
    except ValueError:
        return False


async def _open_for_relay(stream_id: str, stream_url, stream_headers: dict, request: Request):
    if not _is_loopback(request):
        return JSONResponse({"detail": "Only the relay on this server"}, status_code=403)
    provider = live_providers.for_stream_url(stream_url)
    if provider is not None:
        # The provider's current playback headers win over the ones stored
        # at import: 7 TV La Rioja's changed (15-10) and the box still had
        # only the old Referer.
        stream_headers = {**stream_headers, **provider.PLAY_HEADERS}
    nested = request.query_params.get("u")
    if nested is not None:
        if not hls_manifest.is_signed(nested, request.query_params.get("s", "")):
            return JSONResponse({"detail": "Unsigned URL"}, status_code=403)
        url = nested
    elif provider is not None:
        try:
            url = await asyncio.wait_for(run_in_threadpool(_resolve_once, stream_url), OPEN_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            url = None
        if not url:
            return JSONResponse({"detail": "The broadcaster did not return a live link"}, status_code=502)
    elif str(stream_url or '').lower().startswith(('http://', 'https://')):
        url = stream_url
    else:
        return JSONResponse({"detail": "Not an HTTP stream"}, status_code=404)
    try:
        return await asyncio.wait_for(
            run_in_threadpool(_serve_for_relay, stream_id, url, stream_headers), OPEN_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        return JSONResponse({"detail": "The origin did not answer in time"}, status_code=502)


def _serve_manifest(stream_id: str, url: str, headers: dict):
    try:
        final_url, body, content_type = _fetch_manifest(url, headers)
    except Exception as error:
        return JSONResponse({"detail": f"The origin did not return the manifest: {error}"}, status_code=502)
    text = body.decode('utf-8', 'replace')
    if '#EXTM3U' not in text[:512]:
        # Not a manifest -- an origin that answers with the video itself.
        # Hand it over instead of buffering video here.
        return RedirectResponse(final_url, status_code=302)

    def link(absolute_url: str) -> str:
        return (f"/live/{quote(stream_id, safe='')}?u={quote(absolute_url, safe='')}"
                f"&s={hls_manifest.sign(absolute_url)}")

    served = hls_manifest.serve(text, final_url, link)
    # A plain body, so a Content-Length and never chunked: served this way
    # ffmpeg reads it whole.
    return Response(content=served.encode('utf-8'), media_type=HLS_TYPE)


@live_router.api_route("/{stream_id}", methods=["GET", "HEAD"], include_in_schema=False)
async def open_live_stream(stream_id: str, request: Request, db: Session = Depends(get_db)):
    stream = db.get(AcestreamChannel, stream_id)
    stream_url = getattr(stream, "stream_url", None)
    stream_headers = dict(getattr(stream, "stream_headers", None) or {})
    # Released now, not after the response: resolving takes up to three
    # requests to the broadcaster and must not hold a pooled connection
    # meanwhile (see tuner._engine).
    db.close()
    # Errors are answered as JSON here: a 404 raised as an exception would
    # reach the SPA fallback and come back as index.html with HTTP 200.
    if request.query_params.get("r") == "1":
        return await _open_for_relay(stream_id, stream_url, stream_headers, request)
    # A player reading /live/ itself is someone watching: background checks
    # wait (see app.services.viewing). The relay's own reads above are
    # already counted through its claim.
    viewing.note_live_request()
    if hls_manifest.needs_manifest(stream_url, stream_headers):
        if request.method == "HEAD":
            return Response(status_code=200, media_type=HLS_TYPE)
        # A variant or nested manifest: only a URL we signed when rewriting
        # its master, never one a caller makes up.
        nested = request.query_params.get("u")
        if nested is not None:
            if not hls_manifest.is_signed(nested, request.query_params.get("s", "")):
                return JSONResponse({"detail": "Unsigned manifest URL"}, status_code=403)
        try:
            return await asyncio.wait_for(
                run_in_threadpool(_serve_manifest, stream_id, nested or stream_url, stream_headers),
                OPEN_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            return JSONResponse({"detail": "The origin did not answer in time"}, status_code=502)
    if live_providers.for_stream_url(stream_url) is None:
        return JSONResponse({"detail": "Not a live channel"}, status_code=404)
    if request.method == "HEAD":
        # Kodi gives the HEAD five seconds and then retries it; resolving
        # Mediaset here took longer, and zapping took about 25 s. The HEAD
        # only asks what the link is, and all of these are HLS: answer at
        # once and let the GET resolve.
        return Response(status_code=200, media_type=HLS_TYPE)
    # Resolving makes one to three requests to the broadcaster; never on the
    # event loop.
    try:
        # The resolving thread may outlive the answer; its link is still
        # kept for REUSE_SECONDS, so a retry right after can use it.
        link = await asyncio.wait_for(run_in_threadpool(_resolve_once, stream_url), OPEN_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        link = None
    if not link:
        return JSONResponse({"detail": "The broadcaster did not return a live link"}, status_code=502)
    return RedirectResponse(link, status_code=302)
