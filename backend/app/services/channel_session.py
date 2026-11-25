"""One client watching one TV channel: ``/tuner/channel/<id>.ts``.

A ``ChannelSession`` takes a channel and a client and gives back the bytes to
send. Inside: which sources and in what order (one place, also used by the
recordings), the tuner slot, the opening shared by Kodi's two GETs, the
background refresh of the channel's sources while it plays, the last resort
and the cleanup, done once whichever way the session ends. Reading one
source's bytes goes through the ``SourceReader`` seam (engine or HTTP).

Errors are the session's own: ``NoSources``, ``TunerBusy`` and
``NoSourceStarted``; the route turns them into HTTP answers.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Callable, List, Optional

import anyio
import httpx
from fastapi.concurrency import run_in_threadpool

from app.config.database import SessionLocal
from app.repositories.settings_repository import SettingsRepository
from app.services import tuner_first_source, tuner_playback_service as playback, waiting_card
from app.services.engine_client import EngineClient, EngineRefusedError, EngineUnavailableError
from app.services.source_reader import SourceReader
from app.services.stream_relay import EngineStreamError, RelayRegistry, relay_registry
from app.services.tuner_probe_service import tuner_probe_service
from app.services.tuner_remux import ffmpeg_binary
from app.services.tuner_service import TunerService, TunerSettings

# The lines this session logs have always carried the route's logger name.
logger = logging.getLogger('app.api.endpoints.tuner')

# While a channel plays, its sources are rechecked this often.
SOURCE_REFRESH_SECONDS = 60.0


# --- which sources ------------------------------------------------------------
def relay_candidates(tv_channel_id: int) -> List[str]:
    """The sources a viewer of the channel tries, in order: the relay order
    (``TunerService.relay_stream_ids``: saved preferences, then freshness
    groups ranked by playback history; a recent offline check leaves a source
    out), with the sources that failed in this process recently moved last.
    Recordings choose with this same function."""
    db = SessionLocal()
    try:
        return playback.prefer_recovered_sources(TunerService(db).relay_stream_ids(tv_channel_id))
    finally:
        db.close()


def last_resort_candidates(tv_channel_id: int) -> List[str]:
    """Every active source of the channel, recent offline ones included, for
    the last resort (and for telling a channel with no source at all). Its
    order is ``TunerService.stream_ids``' (preferences, then freshness and
    bitrate, without playback history), not the relay order: kept as it is,
    since unifying them would change which sources the last resort tries."""
    db = SessionLocal()
    try:
        return playback.prefer_recovered_sources(TunerService(db).stream_ids(tv_channel_id))
    finally:
        db.close()


def _setting_on(key: str) -> bool:
    db = SessionLocal()
    try:
        value = SettingsRepository(db).get_setting(key) or "true"
        return str(value).lower() not in ("false", "0", "no")
    finally:
        db.close()


def switch_on_failure() -> bool:
    """Whether a source that stops sending is followed by another one in the
    same response instead of ending it. On unless the setting turns it off
    (chosen on 2026-10-14 for the box)."""
    return _setting_on("tuner_switch_on_failure")


def refresh_while_playing() -> bool:
    """Whether the channel's other sources are checked while it plays. On the
    box (an A55) that coincided with the audio of the playing source stalling
    24 s in; off skips it at open and every 60 s (a failing source still asks
    for a refresh)."""
    return _setting_on("tuner_refresh_while_playing")


# --- what a session needs from the process ------------------------------------
@dataclass(frozen=True)
class TunerEnvironment:
    """Where a session reads the engine, the settings and its sources from.
    The route passes its own readers of the configuration; the rest default
    to the process-wide ones. Each is read when a session needs it."""
    engine: Callable[[], EngineClient]          # raises EngineUnavailableError when there is none
    settings: Callable[[], TunerSettings]
    slots: Callable[[], int]
    client_factory: Callable[..., httpx.AsyncClient]
    switch_on_failure: Callable[[], bool] = switch_on_failure
    refresh_while_playing: Callable[[], bool] = refresh_while_playing
    candidates: Callable[[int], List[str]] = relay_candidates
    last_resort: Callable[[int], List[str]] = last_resort_candidates
    #: Builds the reader of a session from its engine (None: no engine) and
    #: client label. Default: the engine and HTTP adapters.
    reader: Optional[Callable[[Optional[EngineClient], str], SourceReader]] = None
    registry: RelayRegistry = relay_registry
    probes: object = tuner_probe_service
    #: The source loop. Default: tuner_playback_service.relay_ranked_streams.
    relay: Optional[Callable[..., AsyncIterator[bytes]]] = None


class NoSources(Exception):
    """The channel has no active supported source at all."""


class TunerBusy(Exception):
    def __init__(self, limit: int):
        super().__init__(f"All {limit} tuner slots are in use")
        self.limit = limit


class NoSourceStarted(Exception):
    """No source delivered a first byte within the startup budget."""


class ChannelSession:
    """One client (an address) watching one TV channel."""

    def __init__(self, tv_channel_id: int, client: str, env: TunerEnvironment):
        self.tv_channel_id = tv_channel_id
        self.client = client
        self.env = env
        self._candidates: Optional[List[str]] = None
        self._opening: Optional[tuner_first_source.ChannelOpening] = None

    async def check(self) -> None:
        """What a HEAD answers: raises NoSources when the channel has nothing
        to try. Starts no source."""
        candidates = await run_in_threadpool(self.env.candidates, self.tv_channel_id)
        # A person may have chosen the source this opening starts with (the
        # Kodi skin's source list): see tuner_first_source.
        self._opening = tuner_first_source.open_channel(self.client, self.tv_channel_id)
        candidates = self._opening.order(candidates)
        eligible = candidates or await run_in_threadpool(self.env.last_resort, self.tv_channel_id)
        if not eligible:
            raise NoSources("This channel has no active supported sources available")
        self._candidates = candidates

    async def start(self) -> Optional[AsyncIterator[bytes]]:
        """The channel's bytes, from the first source that delivers one: the
        first chunk has already arrived when this returns. None when a newer
        GET of the same client and channel took the opening over before any
        byte. Raises NoSources, TunerBusy or NoSourceStarted (whose cause is
        the last source's error). Closing the iterator ends the session."""
        if self._candidates is None:
            await self.check()
        env, tv_channel_id = self.env, self.tv_channel_id
        candidates, opening = self._candidates, self._opening
        key = (self.client, tv_channel_id)
        if waiting_card.recently(key) == 'end':
            # Kodi's second GET of a channel its first GET just found dead: the
            # end card again, without trying the sources, taking a tuner slot
            # someone zapping elsewhere may need, or opening the engine.
            return waiting_card.end_card_stream(await run_in_threadpool(waiting_card.load_end_card))
        limit = await run_in_threadpool(env.slots)
        label = f"tuner:{self.client}"
        # Kodi's second GET (or the same person back on this channel) goes on
        # from where the GET before it is instead of starting the sources again.
        shared, previous = playback.take_over_opening(key)
        resume_deadline, skip = None, set()
        if previous is not None:
            candidates, resume_deadline, skip = playback.resume_order(candidates, previous)
            logger.info("TV relay opening resumed tv_channel_id=%s from=%s skip=%s", tv_channel_id,
                        previous.current, sorted(skip))
        claim = env.registry.try_open(candidates[0] if candidates else "", label, limit)
        if claim is None:
            playback.end_opening(key, shared)
            raise TunerBusy(limit)
        watch_others = await run_in_threadpool(env.refresh_while_playing)
        refresh = env.probes.start(tv_channel_id) if watch_others else None

        async def refresh_failed_source(content_id: str) -> None:
            nonlocal refresh
            opening.source_failed(content_id)
            refresh = env.probes.start(tv_channel_id)

        async def monitor_alternatives() -> None:
            nonlocal refresh
            while True:
                await asyncio.sleep(SOURCE_REFRESH_SECONDS)
                refresh = env.probes.start(tv_channel_id)

        monitor = asyncio.create_task(monitor_alternatives() if watch_others else asyncio.sleep(0))

        async def refreshed_candidates() -> tuple[List[str], bool]:
            current = opening.without_chosen(await run_in_threadpool(env.candidates, tv_channel_id))
            return current, refresh is not None and not refresh.done()

        iterator = None
        reader: Optional[SourceReader] = None

        async def end() -> None:
            """Everything the session holds, released once. Its first await
            runs shielded: a cancelled request stays cancelled at every
            await (anyio's cancellation is level-triggered), and the rest of
            the cleanup would never run."""
            opening.ended()
            playback.end_opening(key, shared)
            monitor.cancel()
            if not shared.superseded:
                # Otherwise the channel's refresh is feeding the newer GET's sources.
                env.probes.stop_channel(tv_channel_id)
            with anyio.CancelScope(shield=True):
                await asyncio.gather(monitor, return_exceptions=True)
                if iterator is not None:
                    await iterator.aclose()

        try:
            relay_settings = await run_in_threadpool(env.settings)
            experimental_transcoding = relay_settings.experimental_transcoding
            if experimental_transcoding or relay_settings.plex_compatible_audio:
                await run_in_threadpool(ffmpeg_binary)
            try:
                engine = await run_in_threadpool(env.engine)
            except EngineUnavailableError:
                # HTTP sources are independent of the optional playback engine.
                engine = None
            reader = (env.reader(engine, label) if env.reader is not None else
                      playback.source_readers(engine, label, registry=env.registry,
                                              client_factory=env.client_factory))
            # With the waiting card Kodi opens the card before its 30 s limit, so
            # the start may go on for as long as the card lasts and try more sources.
            card_after = relay_settings.waiting_card_seconds
            budget = relay_settings.startup_budget_seconds
            if card_after > 0:
                budget = max(budget, int(waiting_card.CARD_SECONDS) - 2)
            relay = env.relay or playback.relay_ranked_streams
            iterator = relay(
                None, candidates, label, claim, registry=env.registry, reader=reader,
                candidate_provider=refreshed_candidates, on_source_failure=refresh_failed_source,
                experimental_transcoding=experimental_transcoding,
                plex_compatible_audio=relay_settings.plex_compatible_audio,
                attempt_seconds=relay_settings.source_start_seconds,
                startup_budget_seconds=budget,
                switch_midstream=await run_in_threadpool(env.switch_on_failure),
                opening=shared, resume_deadline=resume_deadline, skip=skip,
                last_resort_provider=lambda: run_in_threadpool(env.last_resort, tv_channel_id),
                last_resort_now=previous is not None and previous.last_resort,
                tv_channel_id=tv_channel_id)
            if card_after > 0:
                # Also when the relay gives a playing source up and looks for
                # another: the card instead of the last frame frozen on screen.
                # Not before: a healthy low-bitrate source is silent for up to
                # 16 s between pieces, and the card would cut in every time.
                # And when no source is left, a few seconds of «Sin señal» and a
                # clean end, instead of a 502 and Kodi's «Playback failed».
                iterator = waiting_card.with_waiting_card(
                    iterator, await run_in_threadpool(waiting_card.load_card), after=card_after,
                    midstream_after=playback.STALL_SECONDS, key=key,
                    end_card=await run_in_threadpool(waiting_card.load_end_card),
                    end_errors=(EngineStreamError, EngineUnavailableError, EngineRefusedError))
            first = await anext(iterator)
        except BaseException as exc:
            await end()
            if reader is not None:
                reader.close()
            env.registry.close(claim.id)
            if isinstance(exc, StopAsyncIteration):
                # A newer GET of the channel took the opening over before this
                # one sent anything.
                return None
            if isinstance(exc, (EngineStreamError, EngineUnavailableError)):
                raise NoSourceStarted("No source could start playback within the startup budget") from exc
            raise

        async def body() -> AsyncIterator[bytes]:
            try:
                yield first
                async for chunk in iterator:
                    yield chunk
            except (EngineStreamError, EngineUnavailableError):
                # No source left after playback had started (the midstream
                # switch spent them all, with the waiting card off). Video
                # already went out: end the response instead of breaking the
                # transfer.
                logger.info("TV relay ran out of sources after playback; ending tv_channel_id=%s", tv_channel_id)
            finally:
                await end()
                # The relay releases these itself, but only if it ever started:
                # with the end card sent straight away it never does.
                if any(info.id == claim.id for info in env.registry.active()):
                    env.registry.close(claim.id)
                    if reader is not None:
                        reader.close()

        return body()
