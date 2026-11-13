"""Device playlists: one playlist per device, each with its own groups and channels.

A channel is left out of a device playlist when any of its groups is disabled
there (disabling a group disables its subgroups), unless the channel is enabled
on its own; a channel disabled on its own is left out whatever its groups say.
In the playlist a channel keeps only its enabled groups as tabs; one enabled on
its own with every group disabled keeps all of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.models import ChannelGroup, DevicePlaylist, TVChannel
from app.services.channel_group_service import split_groups, sync_groups


class DevicePlaylistNotFound(LookupError):
    pass


@dataclass
class PlaylistFilter:
    """What one device playlist does to the TV channels of the catalogue."""
    hidden: set[int] = field(default_factory=set)
    # TV channel id -> group-title for the channels whose tabs change.
    titles: dict[int, str] = field(default_factory=dict)

    def shows(self, tv_channel_id: int | None) -> bool:
        return tv_channel_id is None or tv_channel_id not in self.hidden

    def title(self, tv_channel) -> str | None:
        return self.titles.get(tv_channel.id, tv_channel.category)


def _disabled_names(groups: list[ChannelGroup], disabled_ids: set[int]) -> set[str]:
    """Names of the groups disabled directly or through an ancestor."""
    by_id = {g.id: g for g in groups}
    names = set()
    for g in groups:
        seen, node = set(), g
        while node is not None and node.id not in seen:
            if node.id in disabled_ids:
                names.add(g.name)
                break
            seen.add(node.id)
            node = by_id.get(node.parent_id)
    return names


def _join(category: str, names: list[str]) -> str:
    return ("; " if "; " in category else ";").join(names)


def build_filter(db: Session, playlist: DevicePlaylist) -> PlaylistFilter:
    off = _disabled_names(db.query(ChannelGroup).all(), set(playlist.disabled_groups or []))
    enabled = set(playlist.enabled_channels or [])
    disabled = set(playlist.disabled_channels or [])
    result = PlaylistFilter()
    for tv_id, category in db.query(TVChannel.id, TVChannel.category):
        own = split_groups(category)
        on = [name for name in own if name not in off]
        if tv_id in enabled:
            shown = True
        elif tv_id in disabled:
            shown = False
        else:
            shown = len(on) == len(own)
        if not shown:
            result.hidden.add(tv_id)
        elif on and len(on) < len(own):
            result.titles[tv_id] = _join(category, on)
    return result


def get_playlist(db: Session, playlist_id: int) -> DevicePlaylist:
    row = db.get(DevicePlaylist, playlist_id)
    if row is None:
        raise DevicePlaylistNotFound(f"Device playlist {playlist_id} not found")
    return row


def filter_for(db: Session, playlist_id: int | None) -> PlaylistFilter | None:
    return None if playlist_id is None else build_filter(db, get_playlist(db, playlist_id))


def summary(db: Session, playlist: DevicePlaylist) -> dict:
    """How many TV channels the playlist keeps and how many tabs Kodi will show."""
    filt = build_filter(db, playlist)
    tabs, shown, total = set(), 0, 0
    for tv in db.query(TVChannel):
        total += 1
        if filt.shows(tv.id):
            shown += 1
            tabs.update(split_groups(filt.title(tv)))
    return dict(channels=shown, total=total, tabs=len(tabs))


def groups(db: Session) -> list[dict]:
    """Every channel group in tab order, with how many TV channels it holds."""
    rows = sync_groups(db)
    db.commit()
    counts: dict[str, int] = {}
    for (category,) in db.query(TVChannel.category):
        for name in split_groups(category):
            counts[name] = counts.get(name, 0) + 1
    return [dict(id=g.id, name=g.name, parent_id=g.parent_id, position=g.position, channels=counts.get(g.name, 0))
            for g in sorted(rows, key=lambda g: (g.parent_id is not None, g.position, g.id))]


def _clean(db: Session, values, model) -> list[int]:
    wanted = sorted({int(v) for v in values or []})
    if not wanted:
        return []
    known = {row_id for (row_id,) in db.query(model.id).filter(model.id.in_(wanted))}
    return [v for v in wanted if v in known]


def save(db: Session, playlist: DevicePlaylist, *, name=None, owner_id=..., disabled_groups=None,
         enabled_channels=None, disabled_channels=None) -> DevicePlaylist:
    """Update what was given; unknown group or channel ids are dropped.

    A channel cannot be both enabled and disabled on its own: the enabled
    list wins when both name it.
    """
    if name is not None:
        playlist.name = name.strip()
    if owner_id is not ...:
        playlist.owner_id = owner_id or None
    if disabled_groups is not None:
        playlist.disabled_groups = _clean(db, disabled_groups, ChannelGroup)
    if enabled_channels is not None:
        playlist.enabled_channels = _clean(db, enabled_channels, TVChannel)
    if disabled_channels is not None:
        playlist.disabled_channels = _clean(db, disabled_channels, TVChannel)
    on = set(playlist.enabled_channels or [])
    playlist.disabled_channels = [v for v in playlist.disabled_channels or [] if v not in on]
    return playlist
