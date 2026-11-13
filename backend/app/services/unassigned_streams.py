"""Streams without a TV channel: an assignment rule is missing or failing.

Device playlists never list them, so they are reported instead: a count on the
TV dashboard and, after each import, one webhook notification about the ones
that arrived since the previous notification.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.models import AcestreamChannel
from app.repositories.settings_repository import SettingsRepository

#: When the last notification went out (ISO UTC); new streams are counted from there.
NOTICE_SETTING = "unassigned_streams_notified_at"
EXAMPLES = 5
RECENT_DAYS = 7


def _loose(db: Session):
    return db.query(AcestreamChannel).filter(AcestreamChannel.tv_channel_id.is_(None),
                                             AcestreamChannel.is_active.is_(True))


def _examples(query) -> list[str]:
    rows = query.order_by(AcestreamChannel.added_at.desc(), AcestreamChannel.name).limit(EXAMPLES).all()
    return [(row.name or "").strip() or row.id[:8] for row in rows]


def summary(db: Session, now: datetime | None = None) -> dict:
    """Active streams without a TV channel, and those that arrived in the last week."""
    since = (now or datetime.now(timezone.utc)) - timedelta(days=RECENT_DAYS)
    recent = _loose(db).filter(AcestreamChannel.added_at >= since)
    return dict(total=_loose(db).count(), recent=recent.count(), recent_days=RECENT_DAYS, examples=_examples(recent))


def new_since_notice(db: Session, now: datetime | None = None) -> dict | None:
    """The streams without a TV channel added since the last notification, and mark them told.

    None when there are none. The first call only sets the mark: streams older
    than the column have no arrival time and are never reported as new.
    """
    now = now or datetime.now(timezone.utc)
    settings = SettingsRepository(db)
    stored = settings.get_setting(NOTICE_SETTING)
    settings.set_setting(NOTICE_SETTING, now.isoformat())
    if not stored:
        return None
    try:
        since = datetime.fromisoformat(stored)
    except ValueError:
        return None
    new = _loose(db).filter(AcestreamChannel.added_at > since, AcestreamChannel.added_at <= now)
    count = new.count()
    if not count:
        return None
    return dict(new=count, total=_loose(db).count(), examples=_examples(new))
