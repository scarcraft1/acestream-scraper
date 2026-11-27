"""A source that has played well is waited for longer before it is given up.

26-11: the relay left a source the viewer liked after 25 s of silence, and
the two after it died or had no commentary. The viewer asked to wait about a
minute for a source that was playing. A source that never played well keeps
the short wait, so a dead start is still left quickly.
"""
import asyncio
from unittest.mock import Mock

import pytest

from app.services.stream_relay import RelayRegistry
from app.services.tuner_playback_service import relay_ranked_streams


def _quiet_source(silence):
    async def relay(_engine, cid, *args, **kwargs):
        yield cid.encode()
        if cid == 'one':
            await asyncio.sleep(.03)
            yield b'one-playing'
            await asyncio.sleep(silence)
            yield b'one-back'
    return relay


async def _first_three(monkeypatch, silence):
    import app.services.tuner_playback_service as playback
    monkeypatch.setattr(playback, 'STALL_SECONDS', .05)
    monkeypatch.setattr(playback, 'HEALTHY_SECONDS', .01)
    monkeypatch.setattr(playback, 'relay_engine_stream', _quiet_source(silence))
    registry = RelayRegistry()
    claim = registry.try_open('one', 'viewer', 1)
    iterator = relay_ranked_streams(Mock(), ['one', 'two'], 'viewer', claim, registry=registry,
                                    switch_midstream=True)
    try:
        return [await anext(iterator) for _ in range(3)]
    finally:
        await iterator.aclose()


def test_the_wait_is_longer_once_a_source_has_played_well():
    import app.services.tuner_playback_service as playback
    assert playback.stall_seconds(0) == playback.STALL_SECONDS
    assert playback.stall_seconds(playback.HEALTHY_SECONDS) == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_a_playing_source_silent_past_the_short_wait_is_kept(monkeypatch):
    # Silent 0.08 s: past the short wait (0.05) and within the long one (0.12).
    assert await _first_three(monkeypatch, .08) == [b'one', b'one-playing', b'one-back']


@pytest.mark.asyncio
async def test_a_playing_source_silent_past_the_long_wait_is_left(monkeypatch):
    assert await _first_three(monkeypatch, .5) == [b'one', b'one-playing', b'two']
