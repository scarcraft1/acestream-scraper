"""Stable channel numbers by group blocks (pure functions, no database).

Every top-level group owns a block of numbers (100, 200, …), fixed the first
time it is seen and kept when tabs are reordered. A channel keeps its number
while it stays inside its group's block; a new one takes the first free slot.
Channels without a group go from 9000.

Each channel is placed in the block of the group where it first appears when
the groups are walked in tab order, once all of its other groups have already
appeared. That way Kodi, which shows tabs in order of first appearance, keeps
the tab order of the group tree. When no channel of a group can wait, the one
with the fewest unseen groups is placed early (``drags``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

BLOCK = 100
NO_GROUP = 9000


@dataclass
class Group:
    name: str
    parent: str = ""
    position: int = 0
    block: Optional[int] = None
    # Channel ids in the order chosen inside this group; others go after, by number.
    members: list[int] = field(default_factory=list)


@dataclass
class Channel:
    id: int
    groups: list[str]
    order: int  # tie-break inside a group: the current number, or the id
    number: Optional[int] = None  # the number it has now


@dataclass
class Numbering:
    numbers: dict[int, int]
    blocks: dict[str, int]
    order: list[int]
    home: dict[int, Optional[str]]
    overflow: list[str]
    out_of_order: list[str]
    drags: list[tuple[str, int]]


def tree_order(groups: Sequence[Group]) -> list[Group]:
    by_parent: dict[str, list[Group]] = {}
    for g in groups:
        by_parent.setdefault(g.parent or "", []).append(g)
    for kids in by_parent.values():
        kids.sort(key=lambda g: g.position)
    out: list[Group] = []
    seen: set[str] = set()

    def walk(parent: str) -> None:
        for g in by_parent.get(parent, []):
            if g.name in seen:
                continue
            seen.add(g.name)
            out.append(g)
            walk(g.name)

    walk("")
    return out


def number_channels(groups: Sequence[Group], channels: Sequence[Channel]) -> Numbering:
    by_name = {g.name: g for g in groups}
    ordered = tree_order(groups)
    pos = {g.name: i for i, g in enumerate(ordered)}

    def ancestors(name: str) -> list[str]:
        out, seen = [], set()
        g = by_name.get(name)
        while g and g.parent and g.parent not in seen:
            seen.add(g.parent)
            out.append(g.parent)
            g = by_name.get(g.parent)
        return out

    def top(name: Optional[str]) -> Optional[Group]:
        g = by_name.get(name) if name else None
        seen = set()
        while g and g.parent and g.parent not in seen:
            seen.add(g.parent)
            g = by_name.get(g.parent)
        return g

    chans = sorted(channels, key=lambda c: c.order)
    by_id = {c.id: c for c in chans}
    expanded: dict[int, list[str]] = {}
    for c in chans:
        names: set[str] = set()
        for g in c.groups:
            if g in by_name:
                names.add(g)
                names.update(a for a in ancestors(g) if a in pos)
        expanded[c.id] = sorted((n for n in names if n in pos), key=pos.__getitem__)

    seen: set[str] = set()
    placed: set[int] = set()
    out: list[int] = []
    home: dict[int, Optional[str]] = {}
    drags: list[tuple[str, int]] = []

    def place(cid: int, group: str) -> None:
        placed.add(cid)
        out.append(cid)
        home[cid] = group
        seen.update(expanded[cid])

    def key(group: str, cid: int) -> float:
        members = by_name[group].members
        return members.index(cid) if cid in members else 1e6 + by_id[cid].order

    for g in ordered:
        name = g.name

        def in_group() -> list[int]:
            return [c.id for c in chans if c.id not in placed and name in expanded[c.id]]

        def ready(cid: int) -> bool:
            return all(x in seen or x == name for x in expanded[cid])

        candidates = [cid for cid in in_group() if ready(cid)]
        if name not in seen and not candidates and in_group():
            extra = lambda cid: [x for x in expanded[cid] if x not in seen and x != name]  # noqa: E731
            first = min(in_group(), key=lambda cid: (len(extra(cid)), key(name, cid)))
            drags.append((name, first))
            place(first, name)
            candidates = [cid for cid in in_group() if ready(cid)]
        for cid in sorted(candidates, key=lambda cid: key(name, cid)):
            place(cid, name)
    for c in chans:
        if c.id not in placed:
            placed.add(c.id)
            out.append(c.id)
            home[c.id] = None

    tops = [g for g in ordered if not g.parent]
    blocks = {g.name: g.block for g in tops if g.block is not None}
    for g in tops:
        if g.name not in blocks:
            blocks[g.name] = max([0, *blocks.values()]) + BLOCK

    def start(cid: int) -> tuple[int, str]:
        t = top(home.get(cid))
        return (blocks[t.name], t.name) if t else (NO_GROUP, "")

    numbers: dict[int, int] = {}
    used: set[int] = set()
    overflow: list[str] = []
    for cid in out:
        current = by_id[cid].number
        base, _ = start(cid)
        if current is not None and base <= current < base + BLOCK and current not in used:
            numbers[cid] = current
            used.add(current)
    for cid in out:
        if cid in numbers:
            continue
        base, name = start(cid)
        n = base
        while n in used:
            n += 1
        if n >= base + BLOCK and name not in overflow:
            overflow.append(name)
        numbers[cid] = n
        used.add(n)

    out.sort(key=numbers.__getitem__)
    appeared: list[str] = []
    seen_now: set[str] = set()
    for cid in out:
        for g in expanded[cid]:
            if g not in seen_now:
                seen_now.add(g)
                appeared.append(g)
    want = [g.name for g in ordered if g.name in seen_now]
    out_of_order = [g for i, g in enumerate(appeared) if want.index(g) != i]
    return Numbering(numbers, blocks, out, home, overflow, out_of_order, drags)


def reblock(groups: Sequence[Group], numbers: dict[int, int]) -> tuple[dict[str, int], dict[int, int]]:
    """Give top-level groups blocks 100, 200… in tab order; numbers keep their offset in the block."""
    tops = [g for g in tree_order(groups) if not g.parent]
    new_blocks: dict[str, int] = {}
    new_numbers = dict(numbers)
    for i, g in enumerate(tops):
        nb = (i + 1) * BLOCK
        new_blocks[g.name] = nb
        ob = g.block
        if ob is None or ob == nb:
            continue
        for cid, n in numbers.items():
            if ob <= n < ob + BLOCK:
                new_numbers[cid] = nb + (n - ob)
    return new_blocks, new_numbers
