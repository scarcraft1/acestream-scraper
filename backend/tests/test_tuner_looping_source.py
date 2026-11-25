"""A source that keeps repeating itself is left when another one can be tried.

24-11: a source jumped back 27 times in 8 minutes (the same few seconds of a
match over and over) and the relay kept it, because jumping back was only
written down. It was also chosen again 5 minutes later. The relay now leaves
a looping source when another one not tried yet has not failed recently,
keeps it as the last resort, and keeps it behind the others for 30 minutes.
On that night the channel had no other live source: leaving it for dead ones
would have ended the channel, worse than a picture that jumps back.
"""
import asyncio
from unittest.mock import Mock

import pytest

from app.services.stream_relay import EngineStreamError, RelayRegistry
from app.services.tuner_playback_service import relay_ranked_streams

# Each chunk carries a PCR: steady, then two jumps back of 5.5 s (too small
# for an early loop, so it takes the second one to turn jumpy).
_LOOP = (100.0, 100.5, 95.0, 95.5, 90.0, 90.5)


def _ts(pcr_seconds, pid=0x100):
    base = int(pcr_seconds * 90000) & ((1 << 33) - 1)
    pcr = bytes([(base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF, (base >> 1) & 0xFF,
                 ((base & 1) << 7) | 0x7E, 0x00])
    adaptation = bytes([7, 0x10]) + pcr
    head = bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x30])
    packet = head + adaptation + bytes(184 - len(adaptation))
    filler = bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10]) + bytes(184)
    return packet + filler


def _relay(calls, sources):
    async def relay(_engine, content_id, _label, **kwargs):
        calls.append(content_id)
        kind = sources[content_id]
        if kind == 'dead':
            await asyncio.sleep(10)
        for i, t in enumerate(_LOOP if kind == 'loop' else (10.0, 10.5, 11.0, 11.5)):
            yield _ts(t)
    return relay


async def _play(channel, **kwargs):
    registry = RelayRegistry()
    claim = registry.try_open(channel[0], 'viewer', 1)
    received = []
    with pytest.raises(EngineStreamError):
        async for chunk in relay_ranked_streams(Mock(), channel, 'viewer', claim, registry=registry,
                                                attempt_seconds=0.2, startup_budget_seconds=1,
                                                switch_midstream=True, **kwargs):
            received.append(chunk)
    return received


@pytest.mark.asyncio
async def test_a_looping_source_is_left_for_one_not_tried_yet(monkeypatch):
    import app.services.tuner_playback_service as playback
    calls = []
    monkeypatch.setattr(playback, 'relay_engine_stream', _relay(calls, {'loop': 'loop', 'good': 'good'}))
    received = await _play(['loop', 'good'])
    # Four chunks of the loop (left at the second jump back), then all of the
    # good one. When the good one ends too, the loop is the last resort.
    assert calls == ['loop', 'good', 'loop']
    assert len(received) == 4 + 4 + 6
    assert playback.failed_recently('loop')


@pytest.mark.asyncio
async def test_when_the_others_are_dead_the_looping_source_comes_back(monkeypatch):
    import app.services.tuner_playback_service as playback
    calls = []
    monkeypatch.setattr(playback, 'relay_engine_stream', _relay(calls, {'loop': 'loop', 'dead': 'dead'}))
    received = await _play(['loop', 'dead'])
    # Back on the loop after the dead one, and this time it is played to its end.
    assert calls == ['loop', 'dead', 'loop']
    assert len(received) == 4 + 6


@pytest.mark.asyncio
async def test_a_looping_source_alone_is_never_left(monkeypatch):
    import app.services.tuner_playback_service as playback
    calls = []
    monkeypatch.setattr(playback, 'relay_engine_stream', _relay(calls, {'loop': 'loop'}))
    received = await _play(['loop'])
    assert calls == ['loop'] and len(received) == 6


@pytest.mark.asyncio
async def test_a_looping_source_is_not_left_for_one_that_failed_recently(monkeypatch):
    import app.services.tuner_playback_service as playback
    calls = []
    monkeypatch.setattr(playback, 'relay_engine_stream', _relay(calls, {'loop': 'loop', 'good': 'good'}))
    playback.record_source_failure('good')
    await _play(['loop', 'good'])
    assert calls[:1] == ['loop'] and calls.count('loop') == 1
    assert calls.index('good') == 1  # tried only after the loop played out


def test_a_looping_source_stays_behind_the_others_for_half_an_hour(monkeypatch):
    import app.services.tuner_playback_service as playback
    now = [5000.0]
    monkeypatch.setattr(playback.time, 'monotonic', lambda: now[0])
    playback.record_source_failure('loop', playback.JUMPY_COOLDOWN_SECONDS)
    # A plain failure later does not cut the half hour short.
    now[0] += 60
    playback.record_source_failure('loop')
    now[0] += 25 * 60
    assert playback.failed_recently('loop')
    assert playback.prefer_recovered_sources(['loop', 'other']) == ['other', 'loop']
    now[0] += 6 * 60
    assert not playback.failed_recently('loop')


@pytest.mark.asyncio
async def test_a_looping_source_the_viewer_left_is_kept_behind_too(monkeypatch):
    # 24-11: the loop ended with the viewer changing channel, nothing was
    # written against it, and the relay chose it first five minutes later.
    import app.services.tuner_playback_service as playback
    calls = []
    monkeypatch.setattr(playback, 'relay_engine_stream', _relay(calls, {'loop': 'loop'}))
    registry = RelayRegistry()
    claim = registry.try_open('loop', 'viewer', 1)
    stream = relay_ranked_streams(Mock(), ['loop', 'other'], 'viewer', claim, registry=registry,
                                  attempt_seconds=0.2, startup_budget_seconds=1, switch_midstream=True)
    # 'other' failed recently, so the loop is not left; the viewer leaves
    # after the loop has turned jumpy.
    playback.record_source_failure('other')
    for _ in range(5):
        await anext(stream)
    await stream.aclose()
    assert playback.prefer_recovered_sources(['loop', 'other'])[-1] == 'loop'
    with playback._failure_lock:
        assert playback._failures['loop'] > playback._failures['other'] + 1000
