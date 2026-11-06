"""Best-stream-first ordering shared by playlists, the tuner lineup and the
TV-channel API (spec 5.2). Duck-typed: works on ORM rows and DTOs."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Mapping, TypeVar

T = TypeVar("T")


def score_acestream(stream) -> int:
    score = 0
    if getattr(stream, "is_online", None):
        score += 10
    if getattr(stream, "logo", None):
        score += 3
    if getattr(stream, "tvg_id", None):
        score += 2
    if getattr(stream, "tvg_name", None):
        score += 1
    return score


def preference_key(stream) -> tuple[bool, int]:
    """Explicit preferences precede automatic ranking; unordered streams follow."""
    order = getattr(stream, "stream_order", None)
    return (order is None, order or 0)


def stream_sort_key(stream):
    order = getattr(stream, "stream_order", None)
    if order is not None:
        return (0, order, getattr(stream, "id", ""))
    return (1, -score_acestream(stream), getattr(stream, "id", ""))


def sort_streams_curated(streams: Iterable[T]) -> List[T]:
    return sorted(streams, key=stream_sort_key)


# A check older than this ranks a source after every fresher one when the
# relay picks what to try first. The status check runs hourly by default.
FRESH_CHECK_HOURS = 6


def checked_since(stream, since: datetime, until: datetime) -> bool:
    checked = getattr(stream, "last_checked", None)
    if checked is None:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    # A clock correction must not make an observation fresh indefinitely.
    return since <= checked <= until


# Castilian first (20-10: in the test one channel came out in Russian
# and another in German, with Spanish sources online on the same channel).
# Evidence of Castilian: a first audio track tagged Spanish, or a name tag such
# as [ES], (ES), "ES |", Spain, España, castellano. Another language: a country
# tag other than ES ([DE], "UK |", "FR ·"...), audio tracks that are all of
# other languages, or Latin American Spanish (latino, [MX]...), which goes to
# the bottom too: dubbed wrong it is noticed in ten seconds (memory
# castellano-no-latino). Nothing is ever dropped, only reordered.
LANG_CASTILIAN, LANG_UNKNOWN, LANG_OTHER = 0, 1, 2
_SPANISH_AUDIO = {"spa", "es", "esp", "spanish", "español"}
_UNTAGGED_AUDIO = {"", "und", "???", "org", "mul", "zxx", "qaa"}
_COUNTRY = r"(?:ru|de|pl|pt|uk|gb|en|us|fr|it|tr|nl|ro|ua|ar|br|mx|rs|be|gr|hr|cz|hu|bg|se|dk|no|fi|il|in|ca|co|cl|pe|ve|ec|uy)"
_CASTILIAN_NAME = re.compile(
    r"\[(?:es|esp|spa)\]|\((?:es|esp)\)|^\s*es\s*[|:·]|\bcastellano\b|\bespaña\b|\bspain\b", re.I)
_LATIN_NAME = re.compile(r"\blatino\b|\blatam\b|\[(?:lat|mx|ar|co|cl|pe|ve)\]|\b(?:mexico|méxico)\b", re.I)
_OTHER_NAME = re.compile(rf"\[{_COUNTRY}\]|\({_COUNTRY}\)|^\s*{_COUNTRY}\s*[|:·]", re.I)


def _audio_languages(stream) -> List[str]:
    tracks = getattr(stream, "audio_tracks", None)
    if isinstance(tracks, str):
        try:
            tracks = json.loads(tracks)
        except ValueError:
            tracks = None
    if not isinstance(tracks, list):
        return []
    return [str(t.get("language") or "").strip().lower() for t in tracks if isinstance(t, dict)]


def stream_language_rank(stream) -> int:
    """0 Castilian, 1 no evidence, 2 another language or Latin American."""
    name = getattr(stream, "name", None) or ""
    if _LATIN_NAME.search(name):
        return LANG_OTHER
    if _CASTILIAN_NAME.search(name):
        return LANG_CASTILIAN
    if _OTHER_NAME.search(name):
        return LANG_OTHER
    langs = _audio_languages(stream)
    tagged = [lang for lang in langs if lang not in _UNTAGGED_AUDIO]
    if langs and langs[0] in _SPANISH_AUDIO:
        return LANG_CASTILIAN
    if tagged and not any(lang in _SPANISH_AUDIO for lang in tagged):
        return LANG_OTHER
    return LANG_UNKNOWN


def relay_order(streams: Iterable, history: Mapping, now: datetime) -> List[str]:
    """The order in which the relay tries a channel's sources (content IDs).

    Pure: it reads only the streams, their playback ``history`` (content ID
    -> score with ``score`` and ``demoted``, see ``stream_history``) and
    ``now``. A saved preference (#267) comes before all of what follows: the
    user's order for this channel is the relay's order. Then sources seen
    online, recent or not; never checked, or offline from an older check; a
    demoted source after every group, never dropped. A source found offline
    recently is left out.

    Within a group the language decides before freshness (20-10: on
    one channel the only source checked that day was in another language and
    an unmarked one had been online the day before; if that one is gone, the
    relay moves on after one attempt): an official HTTP source first (it opens
    in about a second, an acestream in 3 to 17 s on an A55 box), then
    Castilian, unmarked, other languages and Latino
    (``stream_language_rank``), then a check within ``FRESH_CHECK_HOURS``,
    then the playback history score, then bitrate.
    """
    fresh_since = now - timedelta(hours=FRESH_CHECK_HOURS)
    ranked = []
    for stream in streams:
        fresh = checked_since(stream, fresh_since, now)
        if stream.is_online:
            group = 0
        elif stream.is_online is False and fresh:
            continue
        else:
            group = 1
        past = history.get(stream.id.lower())
        if past is not None and past.demoted:
            group = 2
        score = past.score if past is not None else 0.5
        http = stream.id.startswith("url:")
        ranked.append((*preference_key(stream), group, not http, stream_language_rank(stream), not fresh, -score,
                       -(getattr(stream, "bitrate_bps", None) or 0), stream.id.lower()))
    return list(dict.fromkeys(row[-1] for row in sorted(ranked)))
