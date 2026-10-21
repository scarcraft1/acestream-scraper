"""The waiting card: black with «Conectando…», only when a channel is slow.

Kodi gives up opening a video 30 s after it starts waiting, even if bytes
arrive later, so the relay's whole start had to fit in 28 s: two sources at
most. When nothing has arrived after `after` seconds, this sends the card
instead -- Kodi opens it -- and the relay goes on trying sources; the first
real byte is spliced in raw. Measured on the laptop and on the box (Amlogic,
seen by Óscar, 14-10): Kodi follows the splice to 720p and 1080p, as long as
both the card and the channel carry audio; the card carries AAC silence.

Óscar, 14-10: black with «Conectando…» rather than another channel's
content, and «reduce it as much as possible»: it only appears after the
threshold, never on a channel that starts in time.

The card is one 60 s file, never looped: a loop would send timestamps going
back. It is paced at its own bitrate, with one second ahead so Kodi opens it.
`backend/app/assets/conectando.ts`, made with acestream-tvbox's
herramientas/fabrica-cartas.sh recipe (H.264 720p, AAC silence).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator

import anyio

logger = logging.getLogger(__name__)

CARD_PATH = Path(__file__).resolve().parent.parent / 'assets' / 'conectando.ts'
END_CARD_PATH = Path(__file__).resolve().parent.parent / 'assets' / 'sin-senal.ts'
END_CARD_SECONDS = 8.0
CARD_SECONDS = 60.0
PACKET = 188
STEP_SECONDS = 0.2
# Kodi opens a channel with two GETs, and makes the second only once the
# first has DATA -- headers are not enough. So the second one waited the
# threshold again from zero and the card came at twice the threshold: at
# 20 s that is 40, past Kodi's 30 s limit, and the card was never seen;
# Kodi showed «Playback failed» instead (kodi-local-1, 16-10). What the
# first GET of the same client and channel did in the last few seconds is
# remembered here, and the second one picks it up at once.
RECENT_SECONDS = 5.0
_recent: dict = {}


def _remember(key, what: str) -> None:
    if key is not None:
        _recent[key] = (what, time.monotonic())


def _recently(key) -> str | None:
    seen = _recent.get(key) if key is not None else None
    if seen and time.monotonic() - seen[1] < RECENT_SECONDS:
        return seen[0]
    return None


@lru_cache(maxsize=1)
def load_card() -> bytes:
    data = CARD_PATH.read_bytes()
    return data[:len(data) // PACKET * PACKET]


@lru_cache(maxsize=1)
def load_end_card() -> bytes:
    data = END_CARD_PATH.read_bytes()
    return data[:len(data) // PACKET * PACKET]


async def _paced(card: bytes, seconds: float) -> AsyncIterator[bytes]:
    """`card` at its own bitrate, one second ahead, whole packets."""
    rate = len(card) / seconds
    started = time.monotonic()
    sent = 0
    while sent < len(card):
        due = min(len(card), int((time.monotonic() - started + 1) * rate) // PACKET * PACKET)
        if due > sent:
            yield card[sent:due]
            sent = due
        await asyncio.sleep(STEP_SECONDS)


async def with_waiting_card(inner: AsyncIterator[bytes], card: bytes, *, after: float,
                            midstream_after: float | None = None,
                            card_seconds: float = CARD_SECONDS, key=None,
                            end_card: bytes | None = None, end_errors: tuple = (),
                            end_seconds: float | None = None) -> AsyncIterator[bytes]:
    """`inner` as is while it flows; the card while it is silent.

    Before the first chunk the card comes after `after` seconds. Once video
    has flowed, after `midstream_after` seconds of silence (None: never):
    measured on the box (kodi-0c, 15-10), M+ LaLiga stopped 4 s in and the
    relay spent over a minute finding another source, with Kodi showing the
    last frame. Each silence starts the card from the beginning, and the
    next real chunk -- the same source back, or the next one -- is spliced
    in. The end of `inner` passes through.

    With `end_card`, an error in `end_errors` -- no source left -- sends
    that card and ends cleanly instead of raising: raised before any video,
    it was a 502 and Kodi showed «Playback failed», with an OK to press; a
    clean end goes back to the menu (kodi-local-2, 16-10). Without it, the
    error passes through. `key` (client and channel) is what lets Kodi's
    second GET skip the wait: see RECENT_SECONDS.
    """
    end_seconds = END_CARD_SECONDS if end_seconds is None else end_seconds
    if end_card is not None and _recently(key) == 'end':
        # The first GET already found nothing a moment ago: not again. Its
        # relay never starts, so the route releases the tuner slot itself.
        logger.info('TV relay: no source left a moment ago, end card sent again')
        try:
            async for chunk in _paced(end_card, end_seconds):
                yield chunk
        finally:
            with anyio.CancelScope(shield=True):
                await inner.aclose()
        return
    if _recently(key) == 'card':
        after = 0
    rate = len(card) / card_seconds
    last_data = time.monotonic()
    flowed = False
    sent = 0                      # card bytes sent in the current silence
    pending = asyncio.ensure_future(anext(inner))
    try:
        while True:
            await asyncio.wait({pending}, timeout=STEP_SECONDS)
            if pending.done():
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    return
                except end_errors if end_card is not None else () as exc:
                    logger.info('TV relay ended: no source left, end card sent (%s)', exc)
                    _remember(key, 'end')
                    async for end_chunk in _paced(end_card, end_seconds):
                        yield end_chunk
                    return
                if sent:
                    logger.info('TV relay waiting card ended: video spliced in after %.1f s of silence (%d card bytes sent)',
                                time.monotonic() - last_data, sent)
                    sent = 0
                yield chunk
                flowed = True
                last_data = time.monotonic()
                pending = asyncio.ensure_future(anext(inner))
                continue
            threshold = midstream_after if flowed else after
            if threshold is None:
                continue
            showing = time.monotonic() - last_data - threshold
            if showing < 0 or sent >= len(card):
                continue
            due = min(len(card), int((showing + 1) * rate) // PACKET * PACKET)
            if due > sent:
                if sent == 0:
                    logger.info('TV relay waiting card started after %.1f s without video%s',
                                time.monotonic() - last_data, ' (midstream)' if flowed else '')
                    if not flowed:
                        _remember(key, 'card')
                yield card[sent:due]
                sent = due
    finally:
        # The client left or the source failed: the relay inside closes too,
        # so its engine session and tuner slot are released. Shielded: the
        # server cancels the response with an anyio scope, which cancels
        # every await again, and this cleanup was itself cancelled before
        # the relay closed -- on the box the engine kept downloading after
        # Kodi left, with no stop (kodi-0c, 15-10).
        with anyio.CancelScope(shield=True):
            if not pending.done():
                pending.cancel()
                with contextlib.suppress(BaseException):
                    await pending
            await inner.aclose()
