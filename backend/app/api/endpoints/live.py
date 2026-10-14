"""`/live/{stream_id}`: resolve a broadcaster's live channel when it is opened.

Mounted on the app, not on the API router: a player following a playlist
link cannot send the API token. Gated on TUNER_ALLOWED_NETWORKS instead,
like the tuner routes.

Answers with a redirect to the freshly signed link. The playback headers
travel in the playlist entry (`url|Referer=...`); Kodi keeps sending them
after the redirect and to every segment.
"""
import time

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.models import AcestreamChannel
from app.services import live_providers
from app.services.tuner_network import require_tuner_network

live_router = APIRouter(prefix="/live", dependencies=[Depends(require_tuner_network)], tags=["live"])

# Opening one channel is a burst: Kodi asks with HEAD for the type, then GETs
# twice. Each GET would resolve again -- three requests and about six seconds
# on Mediaset -- so a link is reused for a few seconds. Far below the ~5 minutes
# a Mediaset master stays valid for opening; a later reconnect resolves anew.
REUSE_SECONDS = 30
HLS_TYPE = "application/vnd.apple.mpegurl"
_recent: dict = {}


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


@live_router.api_route("/{stream_id}", methods=["GET", "HEAD"], include_in_schema=False)
async def open_live_stream(stream_id: str, request: Request, db: Session = Depends(get_db)):
    stream = db.get(AcestreamChannel, stream_id)
    stream_url = getattr(stream, "stream_url", None)
    # Released now, not after the response: resolving takes up to three
    # requests to the broadcaster and must not hold a pooled connection
    # meanwhile (see tuner._engine).
    db.close()
    # Errors are answered as JSON here: a 404 raised as an exception would
    # reach the SPA fallback and come back as index.html with HTTP 200.
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
    link = await run_in_threadpool(_resolve_once, stream_url)
    if not link:
        return JSONResponse({"detail": "The broadcaster did not return a live link"}, status_code=502)
    return RedirectResponse(link, status_code=302)
