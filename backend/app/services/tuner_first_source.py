"""«Starts with»: the next opening of a TV channel from one device begins with
the source a person chose (acestream-tvbox #12, #20).

The Kodi skin asks for it with a short request and then opens the channel
through the PVR as always, so ↑ ↓ and the channel's info keep working. The
request belongs to one device (the client's address) and one channel, and
lasts REQUEST_SECONDS from when it was made. It is not used up by the first
GET: Kodi opens a channel with a HEAD and two GETs, and both GETs must start
with the same source. Process memory only; nothing is written to the DB.
"""
from __future__ import annotations

import threading
import time

REQUEST_SECONDS = 30.0

_requests: dict[tuple[str, int], tuple[str, float]] = {}
_lock = threading.Lock()


def request(client: str, tv_channel_id: int, content_id: str) -> None:
    with _lock:
        _requests[(client, tv_channel_id)] = (content_id, time.monotonic())


def cancel(client: str, tv_channel_id: int) -> None:
    with _lock:
        _requests.pop((client, tv_channel_id), None)


def requested(client: str, tv_channel_id: int) -> str | None:
    """The source this device asked the channel to start with, if the
    request is still fresh."""
    with _lock:
        now = time.monotonic()
        for key in [k for k, (_, at) in _requests.items() if now - at >= REQUEST_SECONDS]:
            del _requests[key]
        seen = _requests.get((client, tv_channel_id))
        return seen[0] if seen else None
