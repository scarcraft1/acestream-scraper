"""What the scheduled status pass does while someone watches is a setting.

Pausing (the default) suits a metered connection. With a dedicated checking
engine the pass can go on slowly instead, one source every 30 s, or run pass
after pass; without one, checks would share the playback engine, so the pass
keeps pausing whatever is saved.
"""
import asyncio
import threading

import pytest

from app.repositories.settings_repository import SettingsRepository
from app.services import viewing
from app.services.stream_relay import relay_registry
from app.tasks import channel_status_task

PATH = "/api/v1/config/stream-check-viewing"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from app.services.task_service import task_service
    monkeypatch.setattr(task_service, "shutdown_event", threading.Event())
    monkeypatch.delenv("ENABLE_ACESTREAM_CHECK_ENGINE", raising=False)
    for info in relay_registry.active():
        relay_registry.close(info.id)
    yield
    for info in relay_registry.active():
        relay_registry.close(info.id)


def _clock(monkeypatch):
    clock = {"t": 0.0}

    class Loop:
        def time(self):
            return clock["t"]

    async def sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(channel_status_task.asyncio, "get_running_loop", lambda: Loop())
    monkeypatch.setattr(channel_status_task.asyncio, "sleep", sleep)
    return clock


def _pass(db_session, monkeypatch, mode, *, checker=True):
    from app.models.models import AcestreamChannel
    from app.services.task_service import TaskService
    if checker:
        monkeypatch.setenv("ENABLE_ACESTREAM_CHECK_ENGINE", "true")
    service = TaskService()
    monkeypatch.setattr(channel_status_task, "task_service", service)
    for cid in ("a", "b", "c"):
        db_session.add(AcestreamChannel(id=cid, name=cid, is_active=True))
    SettingsRepository(db_session).set_setting(SettingsRepository.STREAM_CHECK_VIEWING, mode)
    db_session.commit()
    clock = _clock(monkeypatch)
    probed = []

    class Checker:
        def __init__(self, db):
            pass

        def _get_engine_url(self):
            return "http://127.0.0.1:6880"

        async def check_channel_status(self, channel, **kwargs):
            probed.append((channel.id, clock["t"]))
            return {"status": "checked", "is_online": True}

    monkeypatch.setattr(channel_status_task, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(channel_status_task, "ChannelStatusService", Checker)
    monkeypatch.setattr(channel_status_task.notification_service, "channel_status_completed", lambda result: None)
    requeued = []
    monkeypatch.setattr(service, "run_again_in", lambda job, seconds: requeued.append((job, seconds)))
    return service, probed, requeued


def test_slow_goes_on_while_watching_one_source_every_30_s(db_session, monkeypatch):
    _, probed, requeued = _pass(db_session, monkeypatch, "slow")
    monkeypatch.setattr(viewing, "watching", lambda: "tv relay")
    result = channel_status_task.run_channel_status_task()
    # No gap before the first source: the gap follows a source the pass opened.
    assert probed == [("a", 0.0), ("b", 30.0), ("c", 60.0)]
    assert result["checked"] == 3 and "stop_reason" not in result
    assert requeued == []


def test_slow_does_not_wait_when_nobody_watches(db_session, monkeypatch):
    _, probed, _ = _pass(db_session, monkeypatch, "slow")
    monkeypatch.setattr(viewing, "watching", lambda: None)
    channel_status_task.run_channel_status_task()
    assert probed == [("a", 0.0), ("b", 0.0), ("c", 0.0)]


def test_without_a_dedicated_checker_slow_still_pauses(db_session, monkeypatch):
    _, probed, _ = _pass(db_session, monkeypatch, "continuous", checker=False)
    monkeypatch.setattr(viewing, "watching", lambda: "tv relay")
    result = channel_status_task.run_channel_status_task()
    assert probed == []
    assert result["stop_reason"] == "viewing" and result["deferred"] == 3


def test_a_slow_pass_gives_way_to_a_queued_job_while_watching(db_session, monkeypatch):
    service, probed, requeued = _pass(db_session, monkeypatch, "slow")
    monkeypatch.setattr(viewing, "watching", lambda: "tv relay")
    waiting = iter([False, True])
    monkeypatch.setattr(service, "maintenance_waiting", lambda: next(waiting))
    result = channel_status_task.run_channel_status_task()
    assert [cid for cid, _ in probed] == ["a"]
    assert result["stop_reason"] == "viewing" and result["deferred"] == 2
    # It resumes a minute later, after the job it let through.
    assert requeued == [("channel_status", channel_status_task.CONTINUOUS_RESTART_SECONDS)]


def test_continuous_starts_the_next_pass_a_minute_after_one_ends(db_session, monkeypatch):
    _, probed, requeued = _pass(db_session, monkeypatch, "continuous")
    monkeypatch.setattr(viewing, "watching", lambda: None)
    channel_status_task.run_channel_status_task()
    assert len(probed) == 3
    assert requeued == [("channel_status", channel_status_task.CONTINUOUS_RESTART_SECONDS)]


def test_maintenance_waiting_sees_the_job_queued_behind():
    from app.services.task_service import TaskService
    service = TaskService()
    first, second = object(), object()
    service._maintenance_queue.append(first)
    assert not service.maintenance_waiting()
    service._maintenance_queue.append(second)
    assert service.maintenance_waiting()


def test_the_setting_needs_a_dedicated_checker(client, monkeypatch):
    assert client.get(PATH).json() == {"mode": "pause", "dedicated_checker": False}
    assert client.put(PATH, json={"mode": "slow"}).status_code == 422
    assert client.put(PATH, json={"mode": "pause"}).status_code == 200
    monkeypatch.setenv("ENABLE_ACESTREAM_CHECK_ENGINE", "true")
    assert client.put(PATH, json={"mode": "continuous"}).json() == {"mode": "continuous", "dedicated_checker": True}
    assert client.put(PATH, json={"mode": "often"}).status_code == 422
    assert client.get(PATH).json()["mode"] == "continuous"


def test_skipped_sources_cost_no_gap(db_session, monkeypatch):
    # 26-11 on the box: four sources skipped (checked moments ago, or the one
    # being watched) took two minutes of gaps before the pass gave way.
    _, probed, _ = _pass(db_session, monkeypatch, "slow")
    monkeypatch.setattr(viewing, "watching", lambda: "tv relay")
    import app.tasks.channel_status_task as task
    real = task.ChannelStatusService

    class Skipping(real):
        async def check_channel_status(self, channel, **kwargs):
            if channel.id in ("a", "b"):
                return {"status": "skipped"}
            return await super().check_channel_status(channel, **kwargs)

    monkeypatch.setattr(task, "ChannelStatusService", Skipping)
    result = task.run_channel_status_task()
    assert result["skipped"] == 2 and probed == [("c", 0.0)]
