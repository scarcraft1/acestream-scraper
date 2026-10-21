"""The waiting card: «Conectando…» only when a channel is slow to start.

Kodi waits 30 s for the video. When nothing has come after the threshold,
the card goes instead and the relay keeps trying sources; the first real
byte is spliced in. Óscar wants it as rarely as possible: never on a channel
that starts in time.
"""
import asyncio
import time

import pytest

from app.services import waiting_card

CARD = bytes([0x47]) + bytes(187)
CARD = CARD * 100          # 18,800 bytes of whole TS packets


async def _source(delay, chunks=(b'real-1', b'real-2'), fail=None, log=None):
    try:
        await asyncio.sleep(delay)
        if fail:
            raise fail
        for chunk in chunks:
            yield chunk
    finally:
        if log is not None:
            log.append('closed')


@pytest.mark.asyncio
async def test_UN_CANAL_QUE_ARRANCA_A_TIEMPO_NO_VE_LA_CARTA(monkeypatch):
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)
    out = [c async for c in waiting_card.with_waiting_card(_source(0.3), CARD, after=0.5, card_seconds=1)]
    assert out == [b'real-1', b'real-2']


@pytest.mark.asyncio
async def test_UNO_LENTO_VE_LA_CARTA_Y_LUEGO_EL_CANAL(monkeypatch, caplog):
    """And the log says so: on the bench nobody could tell «not sent» from
    «sent and not painted» (kodi-0c, 15-10)."""
    caplog.set_level('INFO', logger='app.services.waiting_card')
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)
    out = [c async for c in waiting_card.with_waiting_card(_source(0.4), CARD, after=0.1, card_seconds=10)]
    card_bytes = b''.join(out[:-2])
    assert out[-2:] == [b'real-1', b'real-2']
    assert card_bytes and CARD.startswith(card_bytes)
    assert len(card_bytes) % 188 == 0       # whole packets only, so the splice is clean
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith('TV relay waiting card started') for m in messages)
    assert any(m.startswith('TV relay waiting card ended') and f'({len(card_bytes)} card bytes sent)' in m for m in messages)


@pytest.mark.asyncio
async def test_LA_CARTA_VA_A_SU_RITMO_Y_NO_DE_GOLPE(monkeypatch):
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)
    sent = []
    stream = waiting_card.with_waiting_card(_source(5), CARD, after=0, card_seconds=10)
    started = time.monotonic()
    async for chunk in stream:
        sent.append(len(chunk))
        if time.monotonic() - started > 0.5:
            break
    await stream.aclose()
    # 1 s ahead + 0.5 s at 1/10 of the card per second: ~15 % of it, far from all.
    assert 0.05 * len(CARD) < sum(sent) < 0.3 * len(CARD)


@pytest.mark.asyncio
async def test_SI_FALLAN_TODAS_EL_ERROR_SIGUE_SALIENDO(monkeypatch):
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)
    stream = waiting_card.with_waiting_card(_source(0.2, fail=RuntimeError('no source')), CARD, after=0.05, card_seconds=10)
    with pytest.raises(RuntimeError, match='no source'):
        async for _ in stream:
            pass


@pytest.mark.asyncio
async def test_SI_KODI_SE_VA_EL_RELE_DE_DENTRO_SE_CIERRA(monkeypatch):
    """Its engine session and tuner slot must be released."""
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)
    log = []
    stream = waiting_card.with_waiting_card(_source(5, log=log), CARD, after=0, card_seconds=10)
    await anext(stream)                       # Kodi got the card
    await stream.aclose()                     # and closed
    assert log == ['closed']


def test_LA_CARTA_ES_TS_DE_VERDAD():
    card = waiting_card.load_card()
    assert len(card) > 100_000 and len(card) % 188 == 0
    assert all(card[i] == 0x47 for i in range(0, len(card), 188 * 97))


def test_POR_DEFECTO_A_LOS_20_Y_SE_PUEDE_APAGAR(client):
    assert client.get('/api/v1/tuner/settings').json()['waiting_card_seconds'] == 20
    assert client.put('/api/v1/tuner/settings', json={'waiting_card_seconds': 0}).json()['waiting_card_seconds'] == 0
    assert client.get('/api/v1/tuner/settings').json()['waiting_card_seconds'] == 0
    assert client.put('/api/v1/tuner/settings', json={'waiting_card_seconds': 29}).status_code == 422


def test_LA_RUTA_LA_PONE_Y_ALARGA_EL_PLAZO():
    import inspect
    from app.api.endpoints import tuner
    source = inspect.getsource(tuner.tuner_channel)
    assert 'waiting_card.with_waiting_card(' in source
    assert 'budget = max(budget, int(waiting_card.CARD_SECONDS) - 2)' in source


@pytest.mark.asyncio
async def test_SI_KODI_SE_VA_YA_VIENDO_EL_CANAL_TAMBIEN_SE_CIERRA(monkeypatch):
    log = []

    async def playing():
        try:
            yield b'real-1'
            await asyncio.sleep(10)
            yield b'real-2'
        finally:
            log.append('closed')
    stream = waiting_card.with_waiting_card(playing(), CARD, after=5, card_seconds=10)
    assert await anext(stream) == b'real-1'
    await stream.aclose()
    assert log == ['closed']


# --- once the channel is playing ---------------------------------------------

async def _playing(gaps, log=None):
    """b'real-0', then after each gap another real chunk."""
    try:
        yield b'real-0'
        for i, gap in enumerate(gaps, 1):
            await asyncio.sleep(gap)
            yield f'real-{i}'.encode()
    finally:
        if log is not None:
            log.append('closed')


def _split(out):
    return [('card' if c.startswith(b'\x47') else c.decode()) for c in out]


@pytest.mark.asyncio
async def test_EN_PLENA_EMISION_UN_SILENCIO_LARGO_SALE_CON_CARTA(monkeypatch):
    """Measured on the box: M+ LaLiga stopped 4 s in and Kodi showed the
    last frame for over a minute while the relay searched."""
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)
    out = [c async for c in waiting_card.with_waiting_card(_playing([0.4]), CARD, after=5,
                                                           midstream_after=0.1, card_seconds=10)]
    kinds = _split(out)
    assert kinds[0] == 'real-0' and kinds[-1] == 'real-1'
    assert set(kinds[1:-1]) == {'card'}


@pytest.mark.asyncio
async def test_UN_HUECO_CORTO_NO_SACA_CARTA(monkeypatch):
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)
    out = [c async for c in waiting_card.with_waiting_card(_playing([0.05, 0.05]), CARD, after=5,
                                                           midstream_after=0.3, card_seconds=10)]
    assert _split(out) == ['real-0', 'real-1', 'real-2']


@pytest.mark.asyncio
async def test_SIN_PEDIRLA_EN_PLENA_EMISION_NO_SALE(monkeypatch):
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)
    out = [c async for c in waiting_card.with_waiting_card(_playing([0.4]), CARD, after=0.05, card_seconds=10)]
    assert _split(out) == ['real-0', 'real-1']


@pytest.mark.asyncio
async def test_CADA_SILENCIO_EMPIEZA_LA_CARTA_DESDE_EL_PRINCIPIO(monkeypatch):
    """A card resumed mid-file would carry timestamps from the last silence."""
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)
    numbered = b''.join(bytes([0x47, i]) + bytes(186) for i in range(100))
    out = [c async for c in waiting_card.with_waiting_card(_playing([0.3, 0.3]), numbered, after=5,
                                                           midstream_after=0.1, card_seconds=10)]
    firsts = [out[i + 1][:2] for i, c in enumerate(out[:-1]) if not c.startswith(b'\x47')]
    assert firsts == [bytes([0x47, 0]), bytes([0x47, 0])]


@pytest.mark.asyncio
async def test_SI_LA_FUENTE_ACABA_DURANTE_LA_CARTA_TERMINA_LIMPIO(monkeypatch):
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)

    async def ends():
        yield b'real-0'
        await asyncio.sleep(0.3)
    out = [c async for c in waiting_card.with_waiting_card(ends(), CARD, after=5, midstream_after=0.1, card_seconds=10)]
    assert _split(out)[0] == 'real-0' and set(_split(out)[1:]) == {'card'}


def test_LA_RUTA_LA_PIDE_EN_PLENA_EMISION():
    import inspect
    from app.api.endpoints import tuner
    # Exactly when the relay gives the source up: before that, a healthy
    # low-bitrate source is just between two pieces.
    assert 'midstream_after=tuner_playback_service.STALL_SECONDS' in inspect.getsource(tuner.tuner_channel)


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['before_video', 'while_playing'])
async def test_SI_EL_SERVIDOR_CANCELA_LA_RESPUESTA_EL_RELE_SE_CIERRA(monkeypatch, when):
    """How Kodi leaves: the server cancels the response with an anyio scope,
    which re-delivers the cancellation at every await. Measured on the box
    (kodi-0c, 15-10, 13:03): after Kodi closed, no stop and no linger, and the
    engine kept downloading -- the wrapper's cleanup was itself cancelled
    before it closed the relay inside."""
    import anyio
    monkeypatch.setattr(waiting_card, 'STEP_SECONDS', 0.02)
    log = []

    async def inner():
        try:
            if when == 'while_playing':
                yield b'real-0'
            await asyncio.sleep(10)
            yield b'never'
        finally:
            await asyncio.sleep(0.01)        # cleanup that has to await, like the engine stop
            log.append('closed')

    got = []

    async def consume():
        async for chunk in waiting_card.with_waiting_card(inner(), CARD, after=5, midstream_after=5, card_seconds=10):
            got.append(chunk)
    with anyio.move_on_after(0.2):
        await consume()
    await asyncio.sleep(0.1)
    assert log == ['closed']
