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

logger = logging.getLogger(__name__)

CARD_PATH = Path(__file__).resolve().parent.parent / 'assets' / 'conectando.ts'
CARD_SECONDS = 60.0
PACKET = 188
STEP_SECONDS = 0.2


@lru_cache(maxsize=1)
def load_card() -> bytes:
    data = CARD_PATH.read_bytes()
    return data[:len(data) // PACKET * PACKET]


async def with_waiting_card(inner: AsyncIterator[bytes], card: bytes, *, after: float,
                            card_seconds: float = CARD_SECONDS) -> AsyncIterator[bytes]:
    """`inner` as is when its first chunk comes within `after` seconds;
    otherwise the card until it does. Errors from `inner` pass through."""
    rate = len(card) / card_seconds
    started = time.monotonic()
    first = asyncio.ensure_future(anext(inner))
    sent = 0
    try:
        while not first.done():
            await asyncio.wait({first}, timeout=STEP_SECONDS)
            if first.done():
                break
            showing = time.monotonic() - started - after
            if showing < 0 or sent >= len(card):
                continue
            due = min(len(card), int((showing + 1) * rate) // PACKET * PACKET)
            if due > sent:
                if sent == 0:
                    logger.info('TV relay waiting card started after %.1f s without video', time.monotonic() - started)
                yield card[sent:due]
                sent = due
        chunk = first.result()
        if sent:
            logger.info('TV relay waiting card ended: source spliced in after %.1f s (%d card bytes sent)',
                        time.monotonic() - started, sent)
        yield chunk
        async for chunk in inner:
            yield chunk
    finally:
        # The client left or the source failed: the relay inside closes too,
        # so its engine session and tuner slot are released.
        if not first.done():
            first.cancel()
            with contextlib.suppress(BaseException):
                await first
        await inner.aclose()
