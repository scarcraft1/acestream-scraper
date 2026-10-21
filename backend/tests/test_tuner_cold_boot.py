"""Switched on for a match: every source is cold and every status is old.

The box is only on now and then, so when it is switched on every source's
status is days old, and a cold acestream takes ~17.4 s to start there,
against the setting's 20 s per source. A proposal (2026-10-15) gave sources
not seen online lately 10 s instead: it would have cut every source of such
a channel before it started, and since a cut stops the engine session, the
next attempt starts cold again. It was reverted before it reached the box.
This test keeps the decision: a stale source gets the full per-source time,
end to end through the channel route. Times are scaled down.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from app.services.tuner_service import TunerService, TunerSettings


def test_a_cold_stale_source_still_opens_the_channel(alembic_client, alembic_db_session, monkeypatch):
    from app.api.endpoints import tuner
    from app.config.settings import get_settings
    from app.models.models import AcestreamChannel, TVChannel
    from app.services.tuner_network import get_tuner_gate
    import app.services.tuner_playback_service as playback
    monkeypatch.setenv('TUNER_ALLOWED_NETWORKS', '*')
    get_settings.cache_clear(); get_tuner_gate.cache_clear()
    try:
        tv = TVChannel(name='M+ LaLiga', is_active=True)
        alembic_db_session.add(tv)
        alembic_db_session.flush()
        days_ago = datetime.now(timezone.utc) - timedelta(days=3)
        # Both last seen three days ago: the first online, the second offline.
        alembic_db_session.add_all([
            AcestreamChannel(id='a' * 40, name='cold', tv_channel_id=tv.id, is_active=True,
                             is_online=True, last_checked=days_ago),
            AcestreamChannel(id='b' * 40, name='other', tv_channel_id=tv.id, is_active=True,
                             is_online=False, last_checked=days_ago),
        ])
        alembic_db_session.commit()
        # 0.3 s per source, the cold start takes 0.255 s: 85 %, like 17.4 s of 20.
        monkeypatch.setattr(TunerService, 'settings', lambda self: TunerSettings(
            friendly_name='t', tuner_count=4, max_channels=450, only_online=False,
            source_start_seconds=0.3, startup_budget_seconds=1, waiting_card_seconds=0))
        monkeypatch.setattr(tuner.tuner_probe_service, 'start', Mock())
        monkeypatch.setattr(tuner, '_engine', lambda: Mock())
        monkeypatch.setattr(tuner, '_setting_on', lambda key: False)
        started = []

        async def cold(_engine, content_id, *args, **kwargs):
            started.append(content_id)
            await asyncio.sleep(0.255)
            yield b'\x47' + bytes(187)
        monkeypatch.setattr(playback, 'relay_engine_stream', cold)
        response = alembic_client.get(f'/tuner/channel/{tv.id}.ts')
        assert response.status_code == 200 and response.content.startswith(b'\x47')
        assert started == ['a' * 40]          # opened by the first source, not cut
    finally:
        monkeypatch.undo()
        get_settings.cache_clear(); get_tuner_gate.cache_clear()
