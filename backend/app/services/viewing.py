"""Whether someone is watching right now, so background work can wait.

The scheduled status pass opens the engine for every source it checks. On a
small box that ran next to the channel being watched: on 2026-10-14 the
hourly pass started at 21:49, in the middle of the match, and was still going
when the box was switched off at 22:58. The pass only skipped the one source
the relay was playing, not the others on the same engine.

Three ways of watching, each seen differently:

- the TV relay (/tuner/channel and /tuner/stream, acestream or HTTP): its
  claim is in ``relay_registry`` from the request until the client leaves;
- the web player: its ``starting``/``ready`` sessions;
- a player reading ``/live/`` itself (a per-source entry of an HTTP stream):
  only its requests show it. Manifests served by us are fetched every few
  seconds while it plays; a broadcaster we redirect to is fetched once, so
  that one counts only for ``LIVE_IDLE_SECONDS`` after it opens.
"""
import time
from typing import Optional

LIVE_IDLE_SECONDS = 30.0

_last_live_request = float("-inf")


def note_live_request() -> None:
    """A player (not our own relay) asked ``/live/`` for something."""
    global _last_live_request
    _last_live_request = time.monotonic()


def watching() -> Optional[str]:
    """What someone is watching through, or None when nobody is."""
    from app.services.stream_relay import relay_registry
    if relay_registry.count_active():
        return "tv relay"
    from app.services.player_service import player_service
    if any(s.state in ("starting", "ready") for s in player_service.list_sessions()):
        return "web player"
    if time.monotonic() - _last_live_request < LIVE_IDLE_SECONDS:
        return "live"
    return None
