"""Log when a relayed MPEG-TS source jumps back (or far ahead) in time.

A viewer saw some sources "jump back" mid-match while the
relay served them without switching: nothing in any log showed it, because
nobody looked at the stream's own clock. This reads the PCR (the MPEG-TS
program clock) from a sample of each chunk and logs a line when it goes back
more than BACK_SECONDS, ahead of the wall clock by more than AHEAD_SECONDS, or
one step ahead by more than STEP_SECONDS. It only reads the bytes, never
changes them.

A step ahead is the stream skipping forward between two samples: once
Kodi lost the audio and waited with a full buffer at the end of each
half while the relay logged nothing, because the 30 s wall-clock bound only
catches far jumps. Bursts do not trip it: a burst brings many chunks quickly,
but each one still spans a fraction of a second of stream time.

A source whose clock goes back JUMPY_BACK_JUMPS times in one viewing repeats
what the viewer already saw; ``jumpy`` lets the relay record that against it.
One jump back is enough when it comes in the first EARLY_SECONDS and is
between EARLY_MIN_BACK and EARLY_MAX_BACK: a source that opens on a loop (an
advert repeating) is usually left before it can loop a second time.
Smaller jumps are jitter, and larger ones are a new clock base, not a loop.

Cost: at most SAMPLE_PACKETS packet headers per chunk, so a small fraction of
the stream on a 1080p source.
"""
import logging
import time
from typing import AsyncIterator, Callable, Optional

logger = logging.getLogger(__name__)

PACKET = 188
SYNC = 0x47
SAMPLE_PACKETS = 64
BACK_SECONDS = 1.0
AHEAD_SECONDS = 30.0
STEP_SECONDS = 3.0
JUMPY_BACK_JUMPS = 2
PCR_WRAP = (1 << 33) / 90000.0  # the 33-bit PCR base wraps every ~26.5 h
QUIET_SECONDS = 10.0             # at most one line per jump burst
EARLY_SECONDS = 30.0             # from the first PCR of this attempt
EARLY_MIN_BACK = 10.0
EARLY_MAX_BACK = 1800.0
# 25-11: a source stepped ahead 3-7 s 52 times in 21 min and the viewer saw
# it stop and jump. 26-11: ten steps in five minutes left a source with ~8
# steps every five minutes that the viewer liked (the one with the commentary
# they wanted) for two worse ones. The viewer (26-11): a step ahead bothers
# only when Kodi waits for it too, and only very often, 15-20 a minute; a
# jump back always bothers (JUMPY_BACK_JUMPS). So a step counts only after a
# wait of VISIBLE_WAIT_SECONDS without the source's clock moving, and the
# source is left at STUTTER_STEPS of those in STUTTER_WINDOW.
VISIBLE_WAIT_SECONDS = 2.0
STUTTER_STEPS = 10
STUTTER_WINDOW = 60.0


def _sync_offset(chunk: bytes) -> Optional[int]:
    for offset in range(min(PACKET, len(chunk) - PACKET)):
        if chunk[offset] == SYNC and chunk[offset + PACKET] == SYNC:
            return offset
    return None


def _pcr(packet: bytes):
    """(pid, seconds) when this packet carries a PCR, else None."""
    if len(packet) < 11 or not packet[3] & 0x20 or packet[4] < 7 or not packet[5] & 0x10:
        return None
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    base = (packet[6] << 25) | (packet[7] << 17) | (packet[8] << 9) | (packet[9] << 1) | (packet[10] >> 7)
    return pid, base / 90000.0


class ClockWatch:
    """Follows one source's PCR and logs its jumps."""

    def __init__(self, content_id: str, clock=time.monotonic):
        self.content_id = content_id
        self._clock = clock
        self._pid = None
        self._last = None
        self._last_at = None
        self._quiet_until = 0.0
        self.back_jumps = 0
        self.early_loop = False
        self.pcr_samples = 0
        self.worst_back = 0.0
        self.forward_steps = 0
        self.waited_steps = 0
        self._step_times = []
        self.first_at = None
        self.last_at = None

    @property
    def jumpy(self) -> bool:
        return self.back_jumps >= JUMPY_BACK_JUMPS or self.early_loop

    @property
    def stuttering(self) -> bool:
        """Stepping ahead often after a wait: the viewer sees the picture
        stop, wait and jump."""
        if self.last_at is None:
            return False
        since = self.last_at - STUTTER_WINDOW
        return sum(1 for t in self._step_times if t >= since) >= STUTTER_STEPS

    @property
    def doubtful(self) -> bool:
        return self.jumpy or self.stuttering

    @property
    def watched_seconds(self) -> float:
        return (self.last_at - self.first_at) if self.first_at is not None else 0.0

    def feed(self, chunk: bytes) -> None:
        offset = _sync_offset(chunk)
        if offset is None:
            return
        end = min(len(chunk), offset + SAMPLE_PACKETS * PACKET)
        for start in range(offset, end - PACKET + 1, PACKET):
            packet = chunk[start:start + PACKET]
            if packet[0] != SYNC:
                return
            found = _pcr(packet)
            if found is None:
                continue
            pid, seconds = found
            if self._pid is None:
                self._pid = pid
            if pid == self._pid:
                self._observe(seconds)

    def _observe(self, seconds: float) -> None:
        now = self._clock()
        if self.first_at is None:
            self.first_at = now
        self.last_at = now
        self.pcr_samples += 1
        if self._last is not None:
            # The 33-bit base wraps both ways: forward when it passes the top,
            # and back when a jump back crosses zero.
            delta = (seconds - self._last + PCR_WRAP / 2) % PCR_WRAP - PCR_WRAP / 2
            elapsed = now - self._last_at
            if delta < -BACK_SECONDS:
                self.back_jumps += 1
                self.worst_back = max(self.worst_back, -delta)
                if now - self.first_at <= EARLY_SECONDS and EARLY_MIN_BACK <= -delta <= EARLY_MAX_BACK:
                    self.early_loop = True
                self._report("TV relay source clock went back %.1f s content_id=%s", -delta)
            elif delta - elapsed > AHEAD_SECONDS:
                self._report("TV relay source clock jumped ahead %.1f s content_id=%s", delta - elapsed)
            elif delta > STEP_SECONDS:
                self.forward_steps += 1
                if elapsed >= VISIBLE_WAIT_SECONDS:
                    self.waited_steps += 1
                    self._step_times = [t for t in self._step_times if t >= now - STUTTER_WINDOW] + [now]
                self._report_step(delta, elapsed)
        self._last, self._last_at = seconds, now

    def _report_step(self, delta: float, elapsed: float) -> None:
        now = self._clock()
        if now >= self._quiet_until:
            logger.info("TV relay source clock stepped ahead %.1f s after %.1f s content_id=%s",
                        delta, elapsed, self.content_id)
            self._quiet_until = now + QUIET_SECONDS

    def _report(self, message: str, seconds: float) -> None:
        now = self._clock()
        if now >= self._quiet_until:
            logger.info(message, seconds, self.content_id)
            self._quiet_until = now + QUIET_SECONDS

    def summary(self) -> None:
        """Always written, so a viewing without jumps can be told from a
        watch that saw no PCR at all (pcr_samples=0)."""
        logger.info("TV relay source clock summary content_id=%s back_jumps=%d worst_back=%.1f s forward_steps=%d"
                    " waited_steps=%d pcr_samples=%d early_loop=%s",
                    self.content_id, self.back_jumps, self.worst_back, self.forward_steps,
                    self.waited_steps, self.pcr_samples, self.early_loop)


class SourceLooping(Exception):
    """The source keeps repeating itself and the caller chose to leave it."""


class SourceStuttering(SourceLooping):
    """The source keeps stepping ahead and the caller chose to leave it."""


async def watch_clock(iterator: AsyncIterator[bytes], content_id: str,
                      watch: Optional[ClockWatch] = None,
                      give_up: Optional[Callable[[], bool]] = None) -> AsyncIterator[bytes]:
    """Pass the chunks through unchanged, watching the source's clock. A
    caller that passes its own ``watch`` can read it when the source ends.

    With ``give_up``, a source that turns ``jumpy`` raises SourceLooping
    (``stuttering``: SourceStuttering)
    when ``give_up()`` is true (24-11: a source looped 27 times in 8 min
    while the relay kept it). It is asked at each chunk, so a source left on
    because nothing else was there is dropped once something is."""
    watch = watch or ClockWatch(content_id)
    try:
        async for chunk in iterator:
            try:
                watch.feed(chunk)
            except Exception:  # the watch must never break playback
                logger.debug("TV relay clock watch failed", exc_info=True)
            if give_up is not None and watch.doubtful and give_up():
                if watch.jumpy:
                    logger.info("TV relay leaving a looping source content_id=%s back_jumps=%d early_loop=%s",
                                content_id, watch.back_jumps, watch.early_loop)
                    raise SourceLooping(content_id)
                logger.info("TV relay leaving a stuttering source content_id=%s forward_steps=%d",
                            content_id, watch.forward_steps)
                raise SourceStuttering(content_id)
            yield chunk
    finally:
        watch.summary()
        await iterator.aclose()
