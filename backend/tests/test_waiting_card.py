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
