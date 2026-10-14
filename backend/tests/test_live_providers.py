"""Broadcasters' live channels: imported from their website, resolved when opened.

No network: `base.http` is replaced by a fake that answers from a table and
fails the test on any URL it was not given.
"""
import pytest

from app.models.models import AcestreamChannel
from app.services import live_providers
from app.services.live_providers import atresplayer, base, mediaset, sietetv, telemadrid
from app.services.m3u_service import M3UService
from app.services.playlist_service import PlaylistService


class FakeHttp:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def _answer(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers or {}), body))
        for prefix, answer in self.answers.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer(url) if callable(answer) else answer
        raise AssertionError(f'unexpected request: {method} {url}')

    def get_json(self, url, headers=None):
        return self._answer('GET', url, headers)

    def post_json(self, url, body, headers=None):
        return self._answer('POST', url, headers, body)

    def get_text(self, url, headers=None):
        return self._answer('GET', url, headers)


@pytest.fixture
def fake(monkeypatch):
    def install(answers):
        http = FakeHttp(answers)
        monkeypatch.setattr(base, 'http', http)
        return http
    monkeypatch.setattr(telemadrid, '_policy_key', None)
    return install


# --- which source and which stream belongs to whom --------------------------

@pytest.mark.parametrize('url, provider', [
    ('https://www.atresplayer.com/directos/', atresplayer),
    ('https://atresplayer.com', atresplayer),
    ('https://www.mediasetinfinity.es/', mediaset),
    ('https://www.7riojaplay.tv/', sietetv),
    ('https://www.telemadrid.es/', telemadrid),
])
def test_LA_WEB_DE_LA_CADENA_SE_RECONOCE(url, provider):
    assert live_providers.for_source_url(url) is provider


@pytest.mark.parametrize('url', [
    'https://evil-atresplayer.com/', 'https://atresplayer.com.example.org/',
    'https://www.tdtchannels.com/lists/tv.m3u', '',
])
def test_UNA_WEB_PARECIDA_NO(url):
    assert live_providers.for_source_url(url) is None


def test_EL_ESQUEMA_DICE_QUIEN_RESUELVE():
    assert live_providers.for_stream_url('mediaset://T5') is mediaset
    assert live_providers.for_stream_url('7riojaplay://x8dhao1') is sietetv
    assert live_providers.for_stream_url('https://cdn.example.com/a.m3u8') is None
    assert live_providers.for_stream_url(None) is None


# --- importing them ---------------------------------------------------------

def _imported(provider):
    return {name: meta for _id, name, meta in
            M3UService().extract_channels_from_content(live_providers.playlist(provider))}


def test_MEDIASET_ENTRA_CON_SU_REFERER_Y_SU_GUIA():
    streams = _imported(mediaset)
    assert set(streams) == {'Telecinco', 'Cuatro', 'FDF', 'Divinity', 'Energy', 'Be Mad', 'Boing'}
    telecinco = streams['Telecinco']
    assert telecinco['stream_url'] == 'mediaset://T5'
    # Without the Referer the manifest answers 403.
    assert telecinco['stream_headers'] == {'Referer': mediaset.REFERER}
    assert telecinco['tvg_id'] == 'Telecinco.TV'
    assert telecinco['group_title'] == 'Generalistas'


def test_ATRESPLAYER_PIDE_SUS_CANALES_Y_SOLO_LOS_DE_TELE(fake):
    fake({f'{atresplayer.API}/client/v1/info/channels': [
        {'title': 'Antena 3', 'id': 'a3'}, {'title': 'Nova', 'id': 'nv'},
        {'title': 'Flooxer', 'id': 'fx'}, {'title': 'laSexta'},
    ]})
    streams = _imported(atresplayer)
    assert set(streams) == {'Antena 3', 'Nova'}
    assert streams['Nova']['stream_url'] == 'atresplayer://nv'
    assert 'stream_headers' not in streams['Nova']
    # dobleM's ids: the only public guide found with all six.
    assert streams['Antena 3']['tvg_id'] == 'Antena 3 HD'
    assert streams['Nova']['tvg_id'] == 'Nova'


def test_LA_WEB_DE_LA_CADENA_NO_SE_DESCARGA(fake, monkeypatch):
    """Scraping the website imports its channels without fetching the page."""
    import asyncio
    from app.models.url_types import RegularURL
    from app.scrapers.http import HTTPScraper

    async def no_fetch(self, url):
        raise AssertionError('the website itself must not be fetched')
    monkeypatch.setattr(HTTPScraper, 'fetch_content', no_fetch)

    async def no_status(self, url, status, error=None):
        return None
    monkeypatch.setattr(HTTPScraper, 'update_url_status', no_status)

    scraper = HTTPScraper(RegularURL('https://www.telemadrid.es/'))
    channels, status = asyncio.run(scraper.scrape())
    assert status == 'OK'
    assert {(name, meta['stream_url']) for _id, name, meta in channels} == {
        ('Telemadrid', 'telemadrid://ref:Live_Telemadrid'), ('La Otra', 'telemadrid://ref:Live_LaOtra')}


# --- the playlist -----------------------------------------------------------

def _stream(stream_url, headers=None):
    return AcestreamChannel(id=M3UService._stream_id(stream_url), name='X',
                            stream_url=stream_url, stream_headers=headers)


def test_LA_LISTA_APUNTA_A_LIVE_CON_SUS_CABECERAS():
    stream = _stream('mediaset://T5', {'Referer': mediaset.REFERER})
    link = PlaylistService._stream_link('http://engine:6878/ace/getstream?id=', stream.id,
                                        stream_url=stream.stream_url, stream_headers=stream.stream_headers,
                                        live_base_url='http://192.168.1.5:8000/')
    assert link == ('http://192.168.1.5:8000/live/' + stream.id.replace(':', '%3A')
                    + '|Referer=https%3A%2F%2Fwww.mediasetinfinity.es%2F')


def test_UN_IPTV_NORMAL_SIGUE_SALIENDO_TAL_CUAL():
    link = PlaylistService._stream_link('http://engine/', 'url:abc',
                                        stream_url='https://cdn.example.com/a.m3u8',
                                        live_base_url='http://192.168.1.5:8000')
    assert link == 'https://cdn.example.com/a.m3u8'


def test_SIN_DIRECCION_PUBLICA_NO_SE_INVENTA_UN_ENLACE():
    with pytest.raises(ValueError):
        PlaylistService._stream_link('http://engine/', 'url:abc', stream_url='mediaset://T5')


def test_LA_LISTA_DEL_SERVIDOR_LLEVA_LA_DIRECCION_PUBLICA(client, db_session):
    db_session.add(_stream('telemadrid://ref:Live_LaOtra'))
    db_session.commit()
    body = client.get('/playlist.m3u?only_online=false').text
    assert '/live/url%3A' in body
    assert 'telemadrid://' not in body


# --- opening them -----------------------------------------------------------

@pytest.fixture
def lan(monkeypatch):
    """The test client is not a LAN address; open the gate like the tuner tests do."""
    from app.config.settings import get_settings
    from app.services.tuner_network import get_tuner_gate
    monkeypatch.setenv("TUNER_ALLOWED_NETWORKS", "*")
    get_settings.cache_clear(); get_tuner_gate.cache_clear()
    yield
    get_settings.cache_clear(); get_tuner_gate.cache_clear()


def test_FUERA_DE_LA_RED_PERMITIDA_NO_SE_ABRE(client, db_session, monkeypatch):
    """Token-free, so gated like the tuner: no outbound request for strangers."""
    stream = _stream('mediaset://T5')
    db_session.add(stream)
    db_session.commit()
    monkeypatch.setattr(live_providers, 'resolve', lambda url: pytest.fail('must not resolve'))
    assert client.get(f'/live/{stream.id}', follow_redirects=False).status_code == 403

def test_ABRIR_REDIRIGE_AL_ENLACE_RECIEN_FIRMADO(lan, client, db_session, monkeypatch):
    stream = _stream('mediaset://T5', {'Referer': mediaset.REFERER})
    db_session.add(stream)
    db_session.commit()
    from app.api.endpoints import live
    monkeypatch.setattr(live, '_recent', {})
    asked = []
    monkeypatch.setattr(live_providers, 'resolve',
                        lambda url: asked.append(url) or 'https://dai.example/master.m3u8?hdnea=1')
    response = client.get(f'/live/{stream.id}', follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['location'] == 'https://dai.example/master.m3u8?hdnea=1'
    assert asked == ['mediaset://T5']


def test_UNA_APERTURA_RESUELVE_UNA_VEZ_Y_EL_HEAD_NO_ESPERA(lan, client, db_session, monkeypatch):
    """Kodi opens with HEAD, then two GETs: one resolution, not three."""
    from app.api.endpoints import live
    stream = _stream('mediaset://T5')
    db_session.add(stream)
    db_session.commit()
    asked = []
    monkeypatch.setattr(live_providers, 'resolve', lambda url: asked.append(url) or f'https://dai/{len(asked)}.m3u8')
    monkeypatch.setattr(live, '_recent', {})
    clock = [1000.0]
    monkeypatch.setattr(live.time, 'monotonic', lambda: clock[0])
    head = client.head(f'/live/{stream.id}', follow_redirects=False)
    # Answered at once, without resolving: Kodi gives it five seconds and
    # Mediaset takes about six.
    assert head.status_code == 200
    assert head.headers['content-type'] == 'application/vnd.apple.mpegurl'
    assert asked == []
    for _ in range(2):
        assert client.get(f'/live/{stream.id}', follow_redirects=False).headers['location'] == 'https://dai/1.m3u8'
    assert len(asked) == 1
    # A reconnect later must sign a new link: an old master answers 403.
    clock[0] += live.REUSE_SECONDS + 1
    assert client.get(f'/live/{stream.id}', follow_redirects=False).headers['location'] == 'https://dai/2.m3u8'


def test_SI_LA_CADENA_NO_DA_ENLACE_ES_UN_502(lan, client, db_session, monkeypatch):
    stream = _stream('mediaset://T5')
    db_session.add(stream)
    db_session.commit()
    from app.api.endpoints import live
    monkeypatch.setattr(live, '_recent', {})
    monkeypatch.setattr(live_providers, 'resolve', lambda url: None)
    assert client.get(f'/live/{stream.id}', follow_redirects=False).status_code == 502


@pytest.mark.parametrize('stream_url', [None, 'https://cdn.example.com/a.m3u8'])
def test_LO_QUE_NO_ES_DE_UNA_CADENA_ES_404_Y_NO_LA_WEB(lan, client, db_session, monkeypatch, stream_url):
    """An unknown id answers JSON 404: the SPA fallback would answer HTTP 200
    with index.html, which a player would try to play."""
    monkeypatch.setattr(live_providers, 'resolve', lambda url: pytest.fail('nothing to resolve'))
    stream_id = 'url:nothing'
    if stream_url:
        stream = _stream(stream_url)
        db_session.add(stream)
        db_session.commit()
        stream_id = stream.id
    response = client.get(f'/live/{stream_id}', follow_redirects=False)
    assert response.status_code == 404
    assert response.headers['content-type'].startswith('application/json')
    assert client.head(f'/live/{stream_id}', follow_redirects=False).status_code == 404


# --- resolving each one -----------------------------------------------------

def test_UN_FALLO_DE_LA_CADENA_NO_ES_UNA_EXCEPCION(fake):
    fake({atresplayer.API: ConnectionError('down')})
    assert live_providers.resolve('atresplayer://nv') is None


def test_ATRESPLAYER_COGE_LA_PRIMERA_FUENTE(fake):
    fake({f'{atresplayer.API}/player/v1/live/nv': {'sourcesLive': [
        {'src': ' '}, {'src': 'https://a/hlsts/nova.m3u8'}, {'src': 'https://a/legacy.m3u8'}]}})
    assert live_providers.resolve('atresplayer://nv') == 'https://a/hlsts/nova.m3u8'


def test_MEDIASET_HACE_LAS_TRES_LLAMADAS(fake):
    http = fake({
        f'{mediaset.API}/idm/v3.0/anonymous/login': {'response': {'sid': 's 1', 'beToken': 'tok'}},
        f'{mediaset.API}/playback/v3.0/check': {'isOk': True, 'response': {'dai': {'assetKey': 'k/1'}}},
        'https://pubads.g.doubleclick.net/ssai/event/': {'stream_manifest': 'https://dai/master.m3u8'},
    })
    assert live_providers.resolve('mediaset://T5') == 'https://dai/master.m3u8'
    login, check, dai = http.calls
    assert check[1].endswith('check?sid=s%201')
    assert check[2]['Authorization'] == 'Bearer tok'
    assert check[3]['channelCode'] == 'T5'
    assert dai[1] == 'https://pubads.g.doubleclick.net/ssai/event/k/1/streams'
    assert dai[2]['Referer'] == mediaset.REFERER


def test_MEDIASET_RECHAZADO_NO_SIGUE(fake):
    http = fake({
        f'{mediaset.API}/idm/v3.0/anonymous/login': {'response': {'sid': 's', 'beToken': 't'}},
        # A refusal is trusted even if the body still carries a key.
        f'{mediaset.API}/playback/v3.0/check': {'isOk': False, 'error': {'message': "doesn't have rights to"},
                                                'response': {'dai': {'assetKey': 'k'}}},
    })
    assert live_providers.resolve('mediaset://AC') is None
    assert len(http.calls) == 2


def test_7TV_SIN_CALIDADES_NO_DA_ENLACE(fake):
    http = fake({'https://www.dailymotion.com/player/metadata/video/x8dhao1': {
        'error': {'title': '7rioja ha limitado su acceso a Dailymotion'}}})
    assert live_providers.resolve('7riojaplay://x8dhao1') is None
    # The video is tied to the broadcaster's domain.
    assert 'embedder=https%3A%2F%2Fwww.7riojaplay.tv%2F' in http.calls[0][1]
    assert http.calls[0][2]['Referer'] == sietetv.WEB


def test_7TV_DA_EL_MAESTRO(fake):
    fake({'https://www.dailymotion.com/player/metadata/video/x8dhao1': {
        'qualities': {'auto': [{'url': 'https://cdndirector/master.m3u8?sec=1'}]}}})
    assert live_providers.resolve('7riojaplay://x8dhao1') == 'https://cdndirector/master.m3u8?sec=1'


def _brightcove(duration=-1):
    return {'duration': duration, 'sources': [
        {'type': 'application/dash+xml', 'src': 'https://bc/live.mpd'},
        {'type': 'application/x-mpegURL', 'src': 'https://bc/live.m3u8'}]}


def test_TELEMADRID_DA_EL_HLS_Y_GUARDA_LA_CLAVE(fake):
    http = fake({telemadrid.PLAYER_JS: 'x={policyKey:"PK1"}',
                 'https://edge.api.brightcove.com/': _brightcove()})
    assert live_providers.resolve('telemadrid://ref:Live_Telemadrid') == 'https://bc/live.m3u8'
    assert live_providers.resolve('telemadrid://ref:Live_LaOtra') == 'https://bc/live.m3u8'
    player_downloads = [c for c in http.calls if c[1] == telemadrid.PLAYER_JS]
    assert len(player_downloads) == 1
    assert http.calls[1][2]['Accept'] == 'application/json;pk=PK1'
    assert http.calls[1][1].endswith('/videos/ref%3ALive_Telemadrid')


def test_TELEMADRID_CON_LA_CLAVE_ROTADA_LA_VUELVE_A_LEER_UNA_VEZ(fake, monkeypatch):
    keys = iter(['x={policyKey:"OLD"}', 'x={policyKey:"NEW"}'])
    def api(url):
        accept = http.calls[-1][2]['Accept']
        if accept.endswith('OLD'):
            raise PermissionError('INVALID_POLICY_KEY')
        return _brightcove()
    http = fake({telemadrid.PLAYER_JS: lambda url: next(keys),
                 'https://edge.api.brightcove.com/': api})
    assert live_providers.resolve('telemadrid://ref:Live_Telemadrid') == 'https://bc/live.m3u8'
    assert telemadrid._policy_key == 'NEW'


def test_TELEMADRID_UNA_GRABACION_NO_PASA_POR_DIRECTO(fake):
    fake({telemadrid.PLAYER_JS: 'x={policyKey:"PK"}',
          'https://edge.api.brightcove.com/': _brightcove(duration=3600000)})
    assert live_providers.resolve('telemadrid://ref:Live_Telemadrid') is None


def test_UNA_CADENA_COLGADA_NO_DEJA_A_KODI_ESPERANDO(lan, client, db_session, monkeypatch):
    """Kodi waits on this GET in its interface thread. With a hung
    broadcaster the remote went dead for 20 s; past the budget it must get
    the 502 it handles cleanly."""
    import time as clock
    from app.api.endpoints import live
    stream = _stream('mediaset://T5')
    db_session.add(stream)
    db_session.commit()
    monkeypatch.setattr(live, '_recent', {})
    monkeypatch.setattr(live, 'OPEN_BUDGET_SECONDS', 0.3)
    monkeypatch.setattr(live_providers, 'resolve', lambda url: clock.sleep(2) or 'https://late/x.m3u8')
    started = clock.monotonic()
    response = client.get(f'/live/{stream.id}', follow_redirects=False)
    assert response.status_code == 502
    assert clock.monotonic() - started < 1.5
