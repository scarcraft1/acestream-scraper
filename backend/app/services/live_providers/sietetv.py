"""7 TV La Rioja, a local channel received over the air in Logroño, which
streams through Dailymotion and is in no public list.

The video is tied to the broadcaster's domain: without its Referer the
metadata has no qualities ("ha limitado su acceso a Dailymotion"), and the
manifest answers 403. Its `sec=` token expires within minutes, so it is
resolved at open time and played with the same Referer.
"""
from typing import Dict, List, Optional
from urllib.parse import quote

from app.services.live_providers import base

SCHEME = '7riojaplay://'
HOSTS = ('7riojaplay.tv',)
GROUP = 'La Rioja'
WEB = 'https://www.7riojaplay.tv/'
META = 'https://www.dailymotion.com/player/metadata/video/%s?embedder=%s'
HEADERS = {'User-Agent': base.USER_AGENT, 'Referer': WEB, 'Origin': WEB.rstrip('/')}
PLAY_HEADERS = {'Referer': WEB}

# Dailymotion id -> (name, guide id). dobleM's id: a week ahead, where
# TDTChannels' 7Rioja.TV has the same programmes but only today.
CHANNELS = {
    'x8dhao1': ('7 TV La Rioja', 'La 7 Rioja'),
}


def channels() -> List[Dict[str, Optional[str]]]:
    return [{'ident': ident, 'name': name, 'tvg_id': tvg_id}
            for ident, (name, tvg_id) in CHANNELS.items()]


def resolve(ident: str) -> Optional[str]:
    """The master manifest: the player picks among 720p, 477p and 180p."""
    data = base.http.get_json(META % (quote(ident), quote(WEB, safe='')), HEADERS)
    qualities = (data.get('qualities') or {}).get('auto') or []
    if not qualities:
        base.log.warning('7riojaplay: %s has no link (%s)', ident,
                         (data.get('error') or {}).get('title') or 'no qualities')
        return None
    return qualities[0].get('url')
