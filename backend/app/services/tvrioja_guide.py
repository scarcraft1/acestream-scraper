"""TV Rioja's own schedule, as an XMLTV guide.

TV Rioja (TVR) publishes no XMLTV and appears in none of the merged guides.
It does publish its week in the iframe the newspaper embeds, plain HTML with
no JavaScript, API or cookies:

    https://proyectos.larioja.com/tvr/dimeprogramacion/dimeprogramacion.php?dia=N

`dia` is undocumented and was worked out by comparison: 0 is today and 1..7
are Monday..Sunday. The page itself never states its date, so the converter
needs the page URL, and one source covers the week through `page_urls()`:
the caller fetches each of those pages and converts each one with its URL.

Three traps:

1. No end times. Each row only has a start; a programme ends when the next
   one starts, and the day's last one at midnight.
2. Times are Spanish local time and the offset is not fixed. The module this
   was ported from wrote a constant `+0200`; from the last Sunday of October
   that is an hour off. Converted with Europe/Madrid and written in UTC.
3. `dia=1..7` is resolved to the NEXT date with that weekday, today included.
   Whether the site shows the coming day or the one already past this week
   was not verified; the schedule is weekly, so a past day is the closest
   available guess for the next one.
"""
import html
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

HOST = "proyectos.larioja.com"
PATH = "/tvr/dimeprogramacion/dimeprogramacion.php"
CHANNEL_ID = "tvrioja.tvr"
CHANNEL_NAME = "TV Rioja"
MADRID = ZoneInfo("Europe/Madrid")

_ROW = re.compile(
    r'class="hora">\s*(\d{1,2}):(\d{2})\s*h?\s*</td>.*?'
    r'class="programa">\s*(.*?)\s*</strong>(.*?)</td>', re.S)
_TAGS = re.compile(r"<[^>]+>")


def is_tvrioja_url(url: str) -> bool:
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    return (host == HOST or host.endswith("." + HOST)) and p.path == PATH


def page_urls(url: str) -> List[str]:
    """The seven day pages (dia=1..7) for one TV Rioja source, no network.

    dia=0 is left out on purpose: it is the same page as today's weekday.
    """
    p = urlparse(url)
    query = {k: v for k, v in parse_qs(p.query).items() if k != "dia"}
    return [urlunparse(p._replace(query=urlencode({**query, "dia": n}, doseq=True)))
            for n in range(1, 8)]


def page_date(url: str, today: date) -> Optional[date]:
    """The date a page is about, from its `dia` (see trap 3)."""
    try:
        n = int(parse_qs(urlparse(url or "").query).get("dia", ["0"])[0])
    except ValueError:
        return None
    if n == 0:
        return today
    if not 1 <= n <= 7:
        return None
    return today + timedelta(days=(n - today.isoweekday()) % 7)


def _clean(text: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", text or "")).split())


def rows(text: str) -> List[Tuple[int, str, str]]:
    """[(minutes after midnight, title, synopsis)] as the page lists them."""
    out = []
    for h, m, title, rest in _ROW.findall(text):
        t = _clean(title)
        if t:
            out.append((int(h) * 60 + int(m), t, _clean(rest)))
    return out


def _utc(day: date, minute: int) -> datetime:
    local = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)
    return local.replace(tzinfo=MADRID).astimezone(timezone.utc)


def _xmltv_time(value: datetime) -> str:
    return value.strftime("%Y%m%d%H%M%S +0000")


def html_to_xmltv(content: bytes, url: str = "", today: Optional[date] = None) -> bytes:
    text = content.decode("utf-8", "replace")
    day = page_date(url, today or datetime.now(MADRID).date())
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<tv generator-info-name="acestream-scraper/tvrioja">',
             "  <channel id=%s><display-name>%s</display-name></channel>"
             % (quoteattr(CHANNEL_ID), escape(CHANNEL_NAME))]
    found = sorted(rows(text)) if day else []
    for i, (minute, title, synopsis) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else 24 * 60      # trap 1
        if end <= minute:
            end = minute + 30
        lines.append("  <programme start=%s stop=%s channel=%s>"
                     % (quoteattr(_xmltv_time(_utc(day, minute))),
                        quoteattr(_xmltv_time(_utc(day, end))), quoteattr(CHANNEL_ID)))
        lines.append('    <title lang="es">%s</title>' % escape(title))
        if synopsis:
            lines.append('    <desc lang="es">%s</desc>' % escape(synopsis))
        lines.append("  </programme>")
    lines.append("</tv>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def merge_xmltv(documents: List[bytes]) -> bytes:
    """One XMLTV document out of the day pages: each channel once, every
    programme. A source is processed as a single document, and processing it
    again would replace what the previous day page left."""
    import xml.etree.ElementTree as ET
    root = ET.Element("tv")
    channels, programmes = {}, []
    for doc in documents:
        tree = ET.fromstring(doc)
        for ch in tree.findall("channel"):
            channels.setdefault(ch.get("id"), ch)
        programmes.extend(tree.findall("programme"))
    root.extend(channels.values())
    root.extend(sorted(programmes, key=lambda p: p.get("start", "")))
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
