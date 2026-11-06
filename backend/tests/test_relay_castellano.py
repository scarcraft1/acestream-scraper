"""The relay tries Castilian sources first, other languages and Latino last.

On 2026-10-20, in Kodi against the bench, one channel came out in Russian and
another in German while Spanish sources were online on the same channels
(names below are synthetic). Castilian first, Latin
American Spanish at the bottom (memory castellano-no-latino). Nothing is
dropped: with no Castilian source the relay still plays what there is.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.models import AcestreamChannel, TVChannel
from app.services.stream_ranking import (LANG_CASTILIAN, LANG_OTHER, LANG_UNKNOWN,
                                         stream_language_rank)
from app.services.tuner_service import TunerService


class S:
    def __init__(self, name, audio=None):
        self.name = name
        self.audio_tracks = audio


def tracks(*langs):
    return [{"index": i, "codec": "aac", "language": lang} for i, lang in enumerate(langs)]


@pytest.mark.parametrize("name,audio,rank", [
    ("CANAL DEPORTES Spain --> Lista A", None, LANG_CASTILIAN),
    ("Canal Uno [ES]", None, LANG_CASTILIAN),
    ("ES | Fútbol Plus", None, LANG_CASTILIAN),
    ("Fútbol Plus castellano", None, LANG_CASTILIAN),
    ("Sport Motor HD --> Lista A", tracks("spa"), LANG_CASTILIAN),
    ("Sport Motor HD --> Lista A", None, LANG_UNKNOWN),
    ("Sport Motor HD", tracks(None), LANG_UNKNOWN),
    ("Sport Motor HD", tracks("und"), LANG_UNKNOWN),
    ("Motor Plus [DE] --> Lista A", None, LANG_OTHER),
    ("Deportes Uno HD [RU] --> Lista B", tracks("spa"), LANG_OTHER),   # the name wins: [RU]
    ("UK | Sports Uno", None, LANG_OTHER),
    ("FR · Sports Dos", None, LANG_OTHER),
    ("Deportes Uno HD", tracks("rus"), LANG_OTHER),
    ("Deportes Uno HD", tracks("deu", "spa"), LANG_UNKNOWN),          # Spanish only as 2nd track
    ("Deportes Tres [MX]", None, LANG_OTHER),
    ("Deportes Cuatro latino", None, LANG_OTHER),
    ("Canal Uno [ES] latino", None, LANG_OTHER),                           # latino wins
    ("Cine Estrenos", None, LANG_UNKNOWN),                        # "Es" inside a word
])
def test_language_rank(name, audio, rank):
    assert stream_language_rank(S(name, audio)) == rank


def test_audio_tracks_stored_as_text_are_read():
    assert stream_language_rank(S("Sport Motor", '[{"language": "spa"}]')) == LANG_CASTILIAN


def _stream(db, key, tv, name, *, audio=None, online=True, hours_ago=1, bitrate=None, http=False):
    checked = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    sid = ("url:" + key * 40) if http else key * 40
    extra = {"stream_url": f"https://tv.example/{key}.m3u8"} if http else {}
    db.add(AcestreamChannel(id=sid, name=name, tv_channel_id=tv.id, is_online=online, is_active=True,
                            bitrate_bps=bitrate, last_checked=checked, audio_tracks=audio, **extra))
    return sid


def _channel(db, name):
    tv = TVChannel(name=name, is_active=True)
    db.add(tv)
    db.flush()
    return tv


def test_deportes_uno_plays_the_unmarked_source_before_the_russian_one(db_session):
    tv = _channel(db_session, "Deportes Uno")
    ru = _stream(db_session, "a", tv, "Deportes Uno HD [RU] --> Lista B", bitrate=8_000_000)
    plain = _stream(db_session, "b", tv, "Deportes Uno --> Lista C", bitrate=3_000_000)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [plain, ru]


def test_castilian_beats_unmarked_and_bitrate_only_breaks_ties(db_session):
    tv = _channel(db_session, "Canal Deportes")
    elcano = _stream(db_session, "a", tv, "Canal Deportes 720p ** --> Lista D", bitrate=9_000_000)
    spain = _stream(db_session, "b", tv, "CANAL DEPORTES Spain --> Lista A", bitrate=2_000_000)
    de = _stream(db_session, "c", tv, "Canal Deportes [DE]", bitrate=20_000_000)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [spain, elcano, de]


def test_among_online_sources_language_beats_freshness(db_session):
    """Deportes Uno on the bench: the only source checked today was Russian, an
    unmarked one was online the day before. The language was chosen."""
    tv = _channel(db_session, "Deportes Uno")
    fresh_ru = _stream(db_session, "a", tv, "Deportes Uno HD [RU] --> Lista B", hours_ago=2)
    old_plain = _stream(db_session, "b", tv, "Deportes Uno --> Lista C", hours_ago=28)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [old_plain, fresh_ru]


def test_freshness_breaks_ties_within_the_same_language(db_session):
    tv = _channel(db_session, "Sport Motor")
    old_es = _stream(db_session, "a", tv, "Sport Motor [ES]", hours_ago=70, bitrate=9_000_000)
    fresh_es = _stream(db_session, "b", tv, "Sport Motor HD [ES]", hours_ago=1, bitrate=2_000_000)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [fresh_es, old_es]


def test_an_unchecked_castilian_source_still_goes_after_the_online_ones(db_session):
    tv = _channel(db_session, "Sport Motor")
    unchecked_es = _stream(db_session, "a", tv, "Sport Motor [ES]", online=None, hours_ago=200)
    online_de = _stream(db_session, "b", tv, "Motor Plus [DE]", hours_ago=30)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [online_de, unchecked_es]


def test_official_http_source_keeps_going_first(db_session):
    """An official broadcaster stream opens in a second: a Castilian-tagged
    acestream must not overtake it."""
    tv = _channel(db_session, "Canal Uno")
    ace_es = _stream(db_session, "a", tv, "Canal Uno [ES]")
    official = _stream(db_session, "b", tv, "Canal Uno", http=True)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [official, ace_es]


def test_only_foreign_sources_are_still_played(db_session):
    tv = _channel(db_session, "Deportes Dos")
    ru = _stream(db_session, "a", tv, "Deportes Dos HD [RU]", bitrate=5_000_000)
    de = _stream(db_session, "b", tv, "Deportes Dos HD [DE]", bitrate=6_000_000)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [de, ru]


def test_a_saved_stream_order_comes_before_the_language(db_session):
    """#267: the order a person saved for the channel is the relay's order; the
    language (and freshness) only rank the streams nobody ordered."""
    tv = _channel(db_session, "Deportes Uno")
    ru = _stream(db_session, "a", tv, "Deportes Uno HD [RU] --> Lista B")
    es = _stream(db_session, "b", tv, "Deportes Uno [ES]")
    latino = _stream(db_session, "c", tv, "Deportes Uno latino", hours_ago=48)
    db_session.flush()
    db_session.get(AcestreamChannel, ru).stream_order = 1
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [ru, es, latino]
