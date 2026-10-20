"""Best-stream-first ordering shared by playlists, the tuner lineup and the
TV-channel API (spec 5.2). Duck-typed: works on ORM rows and DTOs."""
from __future__ import annotations

import json
import re
from typing import Iterable, List, TypeVar

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


def sort_streams_curated(streams: Iterable[T]) -> List[T]:
    return sorted(streams, key=lambda s: (-score_acestream(s), getattr(s, "id", "")))


# Castilian first (Óscar, 20-10: in the test Eurosport 1 came out in Russian
# and DAZN F1 in German, with Spanish sources online on the same channel).
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
