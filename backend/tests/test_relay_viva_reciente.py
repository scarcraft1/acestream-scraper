"""A source the check has just found online goes ahead of a better record.

Playback history demotes a source that played badly; until now a demoted
source went after every other group, even when the status check had just
found it online, so the relay tried sources offline since the day before
first. A check within ``FRESH_CHECK_HOURS`` that found the source online now
outweighs that history; among sources found online, the one that played well
more often goes first. The language still decides before freshness, and a
source that failed in this process a moment ago still goes last.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import tuner_playback_service
from app.services.stream_ranking import relay_order

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def row(key, online, hours_ago, name='Deportes Uno --> Lista A'):
    return SimpleNamespace(id=key * 40, name=name, is_online=online, stream_order=None, bitrate_bps=0,
                           last_checked=NOW - timedelta(hours=hours_ago), last_worked_at=None,
                           failed_checks=0, failing_since=None, audio_tracks=None)


def past(score, demoted=False):
    return SimpleNamespace(score=score, demoted=demoted)


def test_a_demoted_source_just_found_online_goes_before_sources_seen_a_day_ago():
    just_checked = row('a', True, 1)
    online_yesterday = row('b', True, 22)
    offline_yesterday = row('c', False, 22)
    history = {'a' * 40: past(0.15, demoted=True), 'b' * 40: past(0.69), 'c' * 40: past(0.6)}
    assert relay_order([offline_yesterday, online_yesterday, just_checked], history, NOW) == \
        ['a' * 40, 'b' * 40, 'c' * 40]


def test_among_sources_just_found_online_the_one_that_played_well_more_often_goes_first():
    seldom = row('a', True, 1)
    often = row('b', True, 2)
    history = {'a' * 40: past(0.15, demoted=True), 'b' * 40: past(0.67)}
    assert relay_order([seldom, often], history, NOW) == ['b' * 40, 'a' * 40]


def test_a_demoted_source_with_an_old_online_check_still_goes_after_the_others():
    stale = row('a', True, 22)
    offline_yesterday = row('b', False, 22)
    history = {'a' * 40: past(0.15, demoted=True)}
    assert relay_order([stale, offline_yesterday], history, NOW) == ['b' * 40, 'a' * 40]


def test_the_language_still_decides_before_a_fresh_check():
    russian_now = row('a', True, 1, name='Deportes Uno HD [RU] --> Lista B')
    castilian_yesterday = row('b', True, 22, name='Deportes Uno [ES] --> Lista A')
    assert relay_order([russian_now, castilian_yesterday], {}, NOW) == ['b' * 40, 'a' * 40]


def test_a_source_that_failed_a_moment_ago_goes_last_even_if_just_found_online():
    just_checked = row('a', True, 1)
    other = row('b', True, 3)
    history = {'a' * 40: past(0.9), 'b' * 40: past(0.5)}
    order = relay_order([just_checked, other], history, NOW)
    assert order == ['a' * 40, 'b' * 40]
    tuner_playback_service.record_source_failure('a' * 40, cooldown=1800)
    assert tuner_playback_service.prefer_recovered_sources(order) == ['b' * 40, 'a' * 40]
