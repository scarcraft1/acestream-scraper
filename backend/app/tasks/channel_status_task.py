"""
Periodic task for updating channel statuses.
"""
from app.config.database import SessionLocal
from app.services import notification_service
from app.services.channel_status_service import ChannelStatusService, channels_in_check_order
from app.models.models import AcestreamChannel
import logging
import asyncio
from datetime import datetime, timezone
from app.services.probe_queue import ProbePriority
from app.services.task_service import task_service
from app.services import viewing
from app.services.config_service import ConfigService
from app.repositories.settings_repository import SettingsRepository

# How often a paused pass looks again whether someone is still watching.
VIEWING_POLL_SECONDS = 5.0
# How long a pass waits in all before it ends. Scheduled jobs share one FIFO
# queue: a paused pass holds the guide refresh and the scraping behind it, so
# it lets them through after this and the next pass tries again.
VIEWING_MAX_PAUSE_SECONDS = 600.0
# With a dedicated checker the pass may go on while someone watches
# (Settings -> Automation, "slow" or "continuous"): one source every this
# many seconds, so the checker's traffic stays small next to the viewer's.
VIEWING_GAP_SECONDS = 30.0
# "continuous": the next pass starts this long after one ends, and a "slow"
# one this long after it gave way, so jobs queued meanwhile go first.
CONTINUOUS_RESTART_SECONDS = 60.0


async def gap_while_watching(seconds: float) -> bool:
    """Wait ``seconds`` before the next source when someone is watching.

    Returns whether it waited. Ends early on shutdown or when the viewer leaves.
    """
    if viewing.watching() is None:
        return False
    loop = asyncio.get_running_loop()
    until = loop.time() + seconds
    while not task_service.shutdown_event.is_set() and viewing.watching() is not None:
        remaining = until - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(VIEWING_POLL_SECONDS, remaining))
    return True


async def wait_while_watching(logger, max_seconds: float) -> tuple[float, bool]:
    """Hold the pass while someone watches. Returns (seconds waited, gave up).

    It gives up after ``max_seconds`` (VIEWING_MAX_PAUSE_SECONDS in all) so a
    long match does not hold the maintenance queue -- guide refresh and
    scraping wait behind this job. The next pass prioritizes older unchecked
    work using the saved interval and age groups.
    """
    reason = viewing.watching()
    if reason is None:
        return 0.0, False
    logger.info("Channel status pass paused: someone is watching (%s)", reason)
    loop = asyncio.get_running_loop()
    started = loop.time()
    while reason is not None and not task_service.shutdown_event.is_set():
        if loop.time() - started >= max_seconds:
            logger.info("Channel status pass stopped after %.0f s paused; the next one resumes", max_seconds)
            return loop.time() - started, True
        remaining = max(0.0, max_seconds - (loop.time() - started))
        await asyncio.sleep(min(VIEWING_POLL_SECONDS, remaining))
        reason = viewing.watching()
    waited = loop.time() - started
    logger.info("Channel status pass resumed after %.0f s paused", waited)
    return waited, False


def run_channel_status_task():
    db = SessionLocal()
    logger = logging.getLogger("channel_status_task")
    try:
        service = ChannelStatusService(db)
        http_only = not service._get_engine_url()
        channels = channels_in_check_order(db, http_only=http_only)
        if http_only and not channels:
            return {"checked": 0, "skipped": 0, "failed": 0, "message": "No engine configured; status checks are disabled.", "message_code": "CHECK_ENGINE_NOT_CONFIGURED"}
        mode = ConfigService(SettingsRepository(db)).effective_stream_check_viewing()
        db.rollback()
        logger.info("Starting channel status update for %d channels (while watching: %s).", len(channels), mode)

        async def check_all():
            started = datetime.now(timezone.utc)
            skipped = 0
            checked = 0
            failed = 0
            online = 0
            offline = 0
            paused = 0.0
            max_pause = VIEWING_MAX_PAUSE_SECONDS
            stop_reason = None
            # The gap follows a source the pass opened: skipped ones (checked
            # moments ago, or the one being watched) cost the engine nothing.
            opened_last = False
            for channel_id in channels:
                if task_service.shutdown_event.is_set():
                    stop_reason = "shutdown"
                    break
                # Between sources, never in the middle of one: a probe that
                # started finishes and cleans up as before.
                if mode == "pause":
                    waited, gave_up = await wait_while_watching(logger, max_pause - paused)
                    paused += waited
                else:
                    # A slow pass can last hours; while someone watches it
                    # gives way to the jobs queued behind it.
                    gave_up = viewing.watching() is not None and task_service.maintenance_waiting()
                    if not gave_up and opened_last:
                        await gap_while_watching(VIEWING_GAP_SECONDS)
                opened_last = False
                if gave_up or task_service.shutdown_event.is_set():
                    stop_reason = "shutdown" if task_service.shutdown_event.is_set() else "viewing"
                    break
                try:
                    channel = db.get(AcestreamChannel, channel_id)
                    if channel is None:
                        skipped += 1
                        continue
                    result = await service.check_channel_status(
                        channel, priority=ProbePriority.BACKGROUND, scan_started_at=started,
                    )
                    if result['status'] == 'skipped':
                        skipped += 1
                    else:
                        opened_last = True
                        checked += 1
                        if result["is_online"]:
                            online += 1
                        else:
                            offline += 1
                except Exception as exc:
                    failed += 1
                    opened_last = True
                    db.rollback()
                    logger.exception("Channel status check failed channel_id=%s error=%s", channel_id, exc)
                finally:
                    db.rollback()
                    task_service.update_task_progress("channel_status", {
                        "processed": checked + skipped + failed, "total": len(channels),
                        "percent": round(100 * (checked + skipped + failed) / len(channels), 1),
                    })
            result = {
                "online": online,
                "offline": offline,
                "checked": checked,
                "skipped": skipped,
                "failed": failed,
            }
            if stop_reason is not None:
                result.update(deferred=len(channels) - checked - skipped - failed, stop_reason=stop_reason)
            return result

        result = asyncio.run(check_all())
        # A slow pass that gave way resumes after the job it let through,
        # not an interval later; a continuous one always starts again.
        gave_way = mode == "slow" and result.get("stop_reason") == "viewing"
        if channels and (mode == "continuous" or gave_way) and result.get("stop_reason") != "shutdown":
            task_service.run_again_in("channel_status", CONTINUOUS_RESTART_SECONDS)
        logger.info("Channel status task completed result=%s", result)
        notification_service.channel_status_completed(result)
        return result
    except Exception as exc:
        logger.exception("Channel status task failed error=%s", exc)
        notification_service.task_failed('channel_status', exc)
        raise
    finally:
        db.close()
