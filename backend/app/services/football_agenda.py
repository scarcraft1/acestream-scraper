"""Football fixtures with the channel that airs them, as an XMLTV guide.

No general guide places a match on the channel that airs it. Measured on one
day: of 42 football matches, the ones landing on a Spanish DAZN or M+ channel
from any of 17 downloaded guides were zero; the 21 that appeared at all were
on La 1, ESPN, Canal Sur or a Portuguese sports channel.

futbolenlatv.es publishes which channel airs each match, with schema.org
microdata (`itemprop="name"`, `startDate`), about three weeks ahead. This
module turns that page into XMLTV, so it is added and refreshed like any
other guide source. Its robots.txt allows /deporte/futbol and forbids
/agenda, /fecha, /partido and /mes: one request per refresh, on the allowed
path.

Two traps, both of which produced a wrong published figure before:

1. `startDate` is UTC without saying so. `2026-10-02T18:45:00` is the match
   shown as 20:45 in Spain. Parsed as local time it shifts two hours.
2. A match has SEVERAL channels, in <li> elements with different classes,
   and the name is in the `title` attribute. Filtering by class kept one per
   match (exactly 1.00 over 767 matches -- a mean that lands on the integer is
   a broken instrument); the real figure is about 1.45.

And one rule: a match is NOT placed on a channel the page does not name. The
page writes plain «DAZN» 204 times in three weeks against 8 numbered ones;
choosing among DAZN 1-4 would be inventing it. A missing programme is better
than a wrong one.
"""
import html
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

HOST = "futbolenlatv.es"
DEFAULT_DURATION = timedelta(hours=2)   # the page does not give one
ID_PREFIX = "futbolenlatv."

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CHANNEL_LIST = re.compile(r'<ul class="listaCanales">(.*?)</ul>', re.S)
# The name is in `title`, whatever the <li> class. See trap 2.
_CHANNEL = re.compile(r'<li[^>]*title="([^"]*)"')
_NAME = re.compile(r'itemprop="name" content="([^"]*)"')
_START = re.compile(r'itemprop="startDate" content="([^"]*)"')
_COMPETITION = re.compile(r'<label title="([^"]*)"')

# Names that do NOT identify one channel. Not bad channels: ambiguous ones.
AMBIGUOUS = {
    "dazn",                 # 204 times in three weeks; DAZN 1, 2, 3 and 4 exist
    "dazn app gratis",      # the app, not a channel in a grid
    "canal por confirmar",  # the page saying it does not know yet
    "replay",               # a rerun with no time of its own
    "ver en directo", "ver gratis", "acceder", "ver partido",
}

# Spellings of the SAME broadcaster that must become one guide channel, or
# a match appears twice on it at the same time («M+ LALIGA» and
# «M+ LALIGA HDR» both air Alavés - At. Madrid).
SAME_BROADCASTER = {
    "m+ laliga hdr": "m+ laliga",
    "la 1 tve": "la 1",
    "m+ #vamos": "m+ vamos",
    "laliga tv hypermotion": "laliga hypermotion",
}


def is_agenda_url(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host == HOST or host.endswith("." + HOST)


def _utc(value: str) -> Optional[datetime]:
    """`2026-10-02T18:45:00` is UTC without saying so. See trap 1."""
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    if not re.search(r"[+-]\d{2}:?\d{2}$", value):
        value += "+00:00"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


_CALL_TO_ACTION = re.compile(r"\s*:?\s*(ver partido|ver en directo|ver gratis|acceder)\s*$", re.I)


def _clean(name: str) -> str:
    """Display spelling: without what the page adds between parentheses
    (the dial number, «Ver en directo») nor a trailing call to click
    («LALIGA TV Hypermotion: VER PARTIDO»)."""
    s = re.sub(r"\s+", " ", re.sub(r"\s*\(.*?\)", " ", name or "")).strip()
    return _CALL_TO_ACTION.sub("", s).strip(" :-")


def broadcaster_key(name: str) -> Optional[str]:
    """Normalised broadcaster, or None when it cannot place a match.

    None is not a failure: it is the rule of the module.
    """
    s = _clean(name)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"\s+", " ", s).strip(" :-")
    if not s or s in AMBIGUOUS:
        return None
    return SAME_BROADCASTER.get(s, s)


def _slug(key: str) -> str:
    s = key.replace("+", " plus ").replace("#", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def events_from_html(text: str) -> List[Dict]:
    """Every match on the page, with ALL its channels and the start in UTC."""
    events = []
    for row in _ROW.findall(text or ""):
        name, start, channels = _NAME.search(row), _START.search(row), _CHANNEL_LIST.search(row)
        if not (name and start and channels):
            continue
        when = _utc(start.group(1))
        if when is None:
            continue
        competition = _COMPETITION.search(row)
        events.append({
            "title": html.unescape(name.group(1)).strip(),
            "competition": html.unescape(competition.group(1)).strip() if competition else "",
            "channels": [html.unescape(c).strip() for c in _CHANNEL.findall(channels.group(1))],
            "start": when,
        })
    events.sort(key=lambda e: e["start"])
    return events


def _xmltv_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def to_xmltv(events: Iterable[Dict]) -> bytes:
    """One guide channel per broadcaster the page names unambiguously."""
    channels: Dict[str, str] = {}
    programmes = []
    placed = set()
    for event in events:
        for raw in event["channels"]:
            key = broadcaster_key(raw)
            if key is None:
                continue
            xml_id = ID_PREFIX + _slug(key)
            channels.setdefault(xml_id, _clean(raw))
            stamp = (xml_id, event["start"], event["title"])
            if stamp in placed:      # two spellings of one broadcaster
                continue
            placed.add(stamp)
            programmes.append((xml_id, event))
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<tv generator-info-name="futbolenlatv agenda">']
    for xml_id, name in sorted(channels.items()):
        lines.append(f"  <channel id={quoteattr(xml_id)}>"
                     f"<display-name lang=\"es\">{escape(name)}</display-name></channel>")
    for xml_id, event in programmes:
        start = event["start"]
        lines.append(
            f"  <programme start=\"{_xmltv_time(start)}\" "
            f"stop=\"{_xmltv_time(start + DEFAULT_DURATION)}\" channel={quoteattr(xml_id)}>"
            f"<title lang=\"es\">{escape(event['title'])}</title>"
            f"<category lang=\"es\">{escape(event['competition'] or 'Fútbol')}</category>"
            "</programme>")
    lines.append("</tv>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def html_to_xmltv(content: bytes) -> bytes:
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    return to_xmltv(events_from_html(text))
