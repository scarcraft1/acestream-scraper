"""Channel groups in the database and the stable numbers they give TV channels.

A channel's groups are the «;»-separated names in ``TVChannel.category`` (the
same value the playlist writes as group-title). ``channel_groups`` adds what a
name alone cannot hold: tab order, nesting and each top-level block of numbers.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.models import ChannelGroup, TVChannel
from app.services import channel_numbering as numbering


def split_groups(category: str | None) -> list[str]:
    return [g.strip() for g in (category or "").split(";") if g.strip()]


def sync_groups(db: Session) -> list[ChannelGroup]:
    """Create a top-level group, last in order, for every category name that has none."""
    rows = db.query(ChannelGroup).all()
    known = {g.name for g in rows}
    top_positions = [g.position for g in rows if g.parent_id is None]
    next_position = max(top_positions, default=-1) + 1
    for (category,) in db.query(TVChannel.category).order_by(TVChannel.id):
        for name in split_groups(category):
            if name not in known:
                row = ChannelGroup(name=name, parent_id=None, position=next_position, member_order=[])
                db.add(row)
                rows.append(row)
                known.add(name)
                next_position += 1
    db.flush()
    return rows


def _inputs(db: Session) -> tuple[list[ChannelGroup], list[TVChannel], list[numbering.Group], list[numbering.Channel]]:
    rows = sync_groups(db)
    by_id = {g.id: g for g in rows}
    groups = [
        numbering.Group(
            name=g.name,
            parent=by_id[g.parent_id].name if g.parent_id in by_id else "",
            position=g.position,
            block=g.number_block,
            members=[int(x) for x in (g.member_order or [])],
        )
        for g in rows
    ]
    tvs = db.query(TVChannel).order_by(TVChannel.id).all()
    channels = [
        numbering.Channel(
            id=c.id,
            groups=split_groups(c.category),
            order=c.channel_number if c.channel_number is not None else 1_000_000 + c.id,
            number=c.channel_number,
        )
        for c in tvs
    ]
    return rows, tvs, groups, channels


def renumber(db: Session) -> numbering.Numbering:
    """Give every TV channel its number in its group's block; fix blocks seen for the first time."""
    rows, tvs, groups, channels = _inputs(db)
    result = numbering.number_channels(groups, channels)
    for g in rows:
        if g.parent_id is None and g.number_block is None:
            g.number_block = result.blocks.get(g.name)
    for c in tvs:
        c.channel_number = result.numbers[c.id]
    db.commit()
    return result


def reblock(db: Session) -> numbering.Numbering:
    """Put blocks back in tab order (100, 200…); channels keep their place inside the block."""
    rows, tvs, groups, channels = _inputs(db)
    current = {c.id: c.number for c in channels if c.number is not None}
    blocks, moved = numbering.reblock(groups, current)
    for g in rows:
        if g.parent_id is None and g.name in blocks:
            g.number_block = blocks[g.name]
    for c in tvs:
        if c.id in moved:
            c.channel_number = moved[c.id]
    db.flush()
    return renumber(db)


def reorder_group(db: Session, name: str, member_ids: list[int] | None = None) -> dict[int, int]:
    """Save the order chosen inside a group (if given) and renumber its top-level block in that order.

    Only channels of that block change number; they share out the numbers the block already held.
    """
    rows, tvs, groups, channels = _inputs(db)
    by_name = {g.name: g for g in rows}
    if name not in by_name:
        raise KeyError(name)
    if member_ids is not None:
        by_name[name].member_order = [int(x) for x in member_ids]
        for g in groups:
            if g.name == name:
                g.members = list(by_name[name].member_order)
    row = by_name[name]
    by_id = {g.id: g for g in rows}
    seen: set[int] = set()
    while row.parent_id is not None and row.parent_id in by_id and row.parent_id not in seen:
        seen.add(row.parent_id)
        row = by_id[row.parent_id]
    numbers = numbering.reorder_block(groups, channels, row.name)
    for c in tvs:
        c.channel_number = numbers[c.id]
    db.commit()
    return numbers
