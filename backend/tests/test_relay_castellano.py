"""The relay tries Castilian sources first, other languages and Latino last.

On 2026-10-20, in Kodi against the bench, Eurosport 1 came out in Russian and
DAZN F1 in German while Spanish sources were online on the same channels
(names below are real ones from the bench). Óscar: Castilian first, Latin
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
    ("TELEDEPORTE Spain --> New Loop", None, LANG_CASTILIAN),
    ("La 1 [ES]", None, LANG_CASTILIAN),
    ("ES | DAZN LaLiga", None, LANG_CASTILIAN),
    ("M+ LaLiga castellano", None, LANG_CASTILIAN),
    ("DAZN F1 HD --> New Loop", tracks("spa"), LANG_CASTILIAN),
    ("DAZN F1 HD --> New Loop", None, LANG_UNKNOWN),
    ("DAZN F1 HD", tracks(None), LANG_UNKNOWN),
    ("DAZN F1 HD", tracks("und"), LANG_UNKNOWN),
    ("Sky Sport F1 [DE] --> New Loop", None, LANG_OTHER),
    ("Eurosport 1 HD [RU] --> SPORTV", tracks("spa"), LANG_OTHER),   # the name wins: [RU]
    ("UK | TNT Sports 1", None, LANG_OTHER),
    ("FR · beIN SPORTS 1", None, LANG_OTHER),
    ("Eurosport 1 HD", tracks("rus"), LANG_OTHER),
    ("Eurosport 1 HD", tracks("deu", "spa"), LANG_UNKNOWN),          # Spanish only as 2nd track
    ("ESPN [MX]", None, LANG_OTHER),
    ("Fox Sports latino", None, LANG_OTHER),
    ("La 1 [ES] latino", None, LANG_OTHER),                           # latino wins
    ("Movistar Estrenos", None, LANG_UNKNOWN),                        # "Es" inside a word
])
def test_language_rank(name, audio, rank):
    assert stream_language_rank(S(name, audio)) == rank


def test_audio_tracks_stored_as_text_are_read():
    assert stream_language_rank(S("DAZN F1", '[{"language": "spa"}]')) == LANG_CASTILIAN


def _stream(db, key, tv, name, *, audio=None, online=True, hours_ago=1, bitrate=None, http=False):
    checked = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    sid = ("url:" + key * 8) if http else key * 40
    extra = {"stream_url": f"https://tv.example/{key}.m3u8"} if http else {}
    db.add(AcestreamChannel(id=sid, name=name, tv_channel_id=tv.id, is_online=online, is_active=True,
                            bitrate_bps=bitrate, last_checked=checked, audio_tracks=audio, **extra))
    return sid


def _channel(db, name):
    tv = TVChannel(name=name, is_active=True)
    db.add(tv)
    db.flush()
    return tv


def test_eurosport_1_plays_the_unmarked_source_before_the_russian_one(db_session):
    tv = _channel(db_session, "Eurosport 1")
    ru = _stream(db_session, "a", tv, "Eurosport 1 HD [RU] --> SPORTV", bitrate=8_000_000)
    plain = _stream(db_session, "b", tv, "Eurosport 1 --> NEW ERA III", bitrate=3_000_000)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [plain, ru]


def test_castilian_beats_unmarked_and_bitrate_only_breaks_ties(db_session):
    tv = _channel(db_session, "Teledeporte")
    elcano = _stream(db_session, "a", tv, "Teledeporte 720p ** --> ELCANO", bitrate=9_000_000)
    spain = _stream(db_session, "b", tv, "TELEDEPORTE Spain --> New Loop", bitrate=2_000_000)
    de = _stream(db_session, "c", tv, "Teledeporte [DE]", bitrate=20_000_000)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [spain, elcano, de]


def test_freshness_still_comes_first(db_session):
    """A recently-checked foreign source is still tried before an old Castilian one."""
    tv = _channel(db_session, "DAZN F1")
    old_es = _stream(db_session, "a", tv, "DAZN F1 [ES]", hours_ago=70)
    fresh_de = _stream(db_session, "b", tv, "Sky Sport F1 [DE]", hours_ago=1)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [fresh_de, old_es]


def test_official_http_source_keeps_going_first(db_session):
    """An official broadcaster stream opens in a second: a Castilian-tagged
    acestream must not overtake it."""
    tv = _channel(db_session, "La 1")
    ace_es = _stream(db_session, "a", tv, "La 1 [ES]")
    official = _stream(db_session, "b", tv, "La 1", http=True)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [official, ace_es]


def test_only_foreign_sources_are_still_played(db_session):
    tv = _channel(db_session, "Eurosport 2")
    ru = _stream(db_session, "a", tv, "Eurosport 2 HD [RU]", bitrate=5_000_000)
    de = _stream(db_session, "b", tv, "Eurosport 2 HD [DE]", bitrate=6_000_000)
    db_session.commit()
    assert TunerService(db_session).relay_stream_ids(tv.id) == [de, ru]
