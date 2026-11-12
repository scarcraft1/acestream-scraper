"""Apply the channel rules tree to stored streams, as undoable runs.

``channel_tree`` classifies one stream at a time and never touches the database.
This module turns its answers into TV channel assignments:

- The leaf's channel is matched against the existing TV channels by
  ``channel_tree.resolve`` (case, spacing, accents and punctuation ignored, and
  «X 1» is «X» when only «X» exists). An existing name that carries a
  publisher code («Name 28fb») also answers to the name without it. A name that
  matches two channels is left alone and reported.
- Missing channels are created. New names that only differ in case or spacing
  become one channel, named after a stream that brings a guide, else after one
  not written all in capitals.
- A channel gains the groups its streams reach in the tree. Groups are only
  added here, never removed.
- Guides: a guide written in the leaf, or «no guide», is applied. Otherwise a
  channel without a guide takes the tvg-id most of its streams bring, when at
  least two bring it, no other ties with it and a loaded guide has it. A
  channel that already has a guide keeps it; a different majority is only
  reported.
- Streams the tree leaves in a node's remainder keep what they have.

Every write is recorded with its previous value in ``channel_tree_changes`` so
the run can be undone within ``UNDO_DAYS``, newest first: a run is refused while
a later run, not undone, changed one of its streams or channels. Undoing restores
what the run replaced (a moved stream also loses its place in the channel) and
removes the channels the run created when no stream is left on them, unlinking
their recordings as a normal delete does.

Channel numbers are not touched: a created channel has none until numbering
gives it one.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.models.models import (AcestreamChannel, ChannelTreeChange, ChannelTreeRun, EPGChannel, TVChannel,
                               _utcnow)
from app.models.recordings import Recording
from app.repositories.settings_repository import SettingsRepository
from app.services import channel_tree as tree_engine
from app.services.channel_group_service import split_groups

TREE_SETTING = "channel_tree"
AUTO_SETTING = "channel_tree_auto"
UNDO_DAYS = 7
MIN_GUIDE_VOTES = 2
_PUBLISHER_CODE = re.compile(r"\s+[0-9a-f]{3,4}$", re.IGNORECASE)


class TreeRunError(ValueError):
    """A run that cannot be undone, or a tree that cannot be stored."""


class TreeRunNotFound(TreeRunError):
    """No run with that id."""


def list_label(source_url: Optional[str]) -> str:
    """The «list» a stream came from, as the tree's ``list`` condition sees it."""
    url = (source_url or "").strip()
    if not url or url == "manual":
        return "manual"
    if url.startswith("telegram-source:"):
        return "Telegram"
    parts = urlsplit(url)
    if parts.scheme == "zero":
        return "ZeroNet"
    if parts.hostname:
        return parts.hostname
    return url.split(":", 1)[0]


def source_of(row: AcestreamChannel) -> dict[str, Any]:
    return {
        "name": row.name or "",
        "tvg": row.tvg_id or "",
        "group": row.group or "",
        "kind": "http" if (row.id or "").startswith("url:") else "acestream",
        "list": list_label(row.source_url),
        "cid": row.id or "",
    }


@dataclass
class Placement:
    """Where the tree sends one stream."""
    stream_id: str
    status: str  # "channel", "none" (leaf without channel), "rest" (no leaf)
    channel: Optional[str] = None
    guide: Optional[str] = ""  # None: no guide; "": automatic
    groups: list[str] = field(default_factory=list)
    tvg: str = ""
    # Ids of the nodes the stream went through, root first; the last one is the leaf or the
    # node whose children it did not match.
    path: list[str] = field(default_factory=list)
    # How the leaf's channel was found: existing, publisher_code, new or ambiguous.
    how: str = ""


@dataclass
class Plan:
    placements: list[Placement]
    # Leaf channel name (as the tree writes it) -> existing TV channel id, or None when it is new.
    targets: dict[str, Optional[int]]
    # Normalised key of a new channel -> the name it will be created with.
    new_names: dict[str, str]
    ambiguous: dict[str, list[str]]


def _labels(root: Optional[dict[str, Any]]) -> dict[str, str]:
    """Node id -> label, in tree order (the id when a node has no label)."""
    labels: dict[str, str] = {}

    def walk(node: dict[str, Any]) -> None:
        nid = node.get("id")
        if isinstance(nid, str):
            labels[nid] = node.get("label") or nid
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child)

    if isinstance(root, dict):
        walk(root)
    return labels


class ChannelTreeService:
    def __init__(self, db: Session):
        self.db = db
        self.settings = SettingsRepository(db)
        self.last_plan: Optional[Plan] = None  # what the last apply decided, for the preview

    # ------------------------------------------------------------ the tree
    def tree(self) -> Optional[dict[str, Any]]:
        raw = self.settings.get_setting(TREE_SETTING)
        if not raw:
            return None
        try:
            root = json.loads(raw)
        except ValueError:
            return None
        return root if isinstance(root, dict) else None

    def save_tree(self, root: dict[str, Any]) -> None:
        problems = tree_engine.tree_problems(root)
        if problems:
            raise TreeRunError("; ".join(problems))
        self.settings.set_setting(TREE_SETTING, json.dumps(root, ensure_ascii=False))

    def auto_enabled(self) -> bool:
        return str(self.settings.get_setting(AUTO_SETTING, "false")).strip().lower() in ("1", "true", "yes", "on")

    # ------------------------------------------------------------ planning
    def plan(self, root: dict[str, Any], rows: Iterable[AcestreamChannel]) -> Plan:
        placements = []
        for row in rows:
            st = tree_engine.run(root, source_of(row))
            if st.leaf is None:
                placements.append(Placement(row.id, "rest", tvg=row.tvg_id or "", path=list(st.path)))
            elif st.leaf.get("none") or not st.leaf.get("channel"):
                placements.append(Placement(row.id, "none", tvg=row.tvg_id or "", path=list(st.path)))
            else:
                placements.append(Placement(row.id, "channel", st.leaf["channel"], st.leaf.get("guide", ""),
                                            list(st.groups), row.tvg_id or "", list(st.path)))

        existing = self.db.query(TVChannel.id, TVChannel.name).all()
        names = [name for _id, name in existing]
        id_of = {name: cid for cid, name in existing}
        # «Name 28fb»: a channel stored with the publisher's code also answers to «Name».
        aliases: dict[str, str] = {}
        for name in names:
            bare = _PUBLISHER_CODE.sub("", name)
            if bare != name:
                aliases.setdefault(bare, name)

        targets: dict[str, Optional[int]] = {}
        by_alias: set[str] = set()
        ambiguous: dict[str, list[str]] = {}
        new_forms: dict[str, list[Placement]] = defaultdict(list)
        for p in placements:
            if p.status != "channel" or p.channel in ambiguous:
                continue
            if p.channel in targets:
                if targets[p.channel] is None:
                    new_forms[tree_engine.norm(p.channel)].append(p)
                continue
            hit = tree_engine.resolve(p.channel, names)
            if hit["status"] == "new" and aliases:
                alias = tree_engine.resolve(p.channel, list(aliases))
                if alias["status"] == "ok":
                    hit = {"status": "ok", "name": aliases[alias["name"]]}
                    by_alias.add(p.channel)
            if hit["status"] == "ok":
                targets[p.channel] = id_of[hit["name"]]
            elif hit["status"] == "amb":
                ambiguous[p.channel] = list(hit["hits"])
            else:
                targets[p.channel] = None
                new_forms[tree_engine.norm(p.channel)].append(p)

        for p in placements:
            if p.status == "channel":
                p.how = ("ambiguous" if p.channel in ambiguous else "new" if targets.get(p.channel) is None
                         else "publisher_code" if p.channel in by_alias else "existing")
        new_names = {key: self._new_name(forms) for key, forms in new_forms.items()}
        return Plan(placements, targets, new_names, ambiguous)

    @staticmethod
    def _new_name(forms: list[Placement]) -> str:
        with_guide = [p.channel for p in forms if p.tvg]
        if with_guide:
            return with_guide[0]
        mixed = [p.channel for p in forms if p.channel != p.channel.upper()]
        return (mixed or [forms[0].channel])[0]

    # ------------------------------------------------------------ applying
    def apply(self, stream_ids: Optional[Iterable[str]] = None, trigger: str = "manual",
              root: Optional[dict[str, Any]] = None) -> Optional[ChannelTreeRun]:
        """Apply the tree to ``stream_ids`` (all streams when None). Flushes, does not commit.

        Returns the run, or None when there is no tree or nothing to look at. An automatic run
        that changes nothing is not kept and also returns None.
        """
        root = root if root is not None else self.tree()
        if root is None:
            return None
        self.db.flush()  # streams the caller has just added
        query = self.db.query(AcestreamChannel)
        if stream_ids is not None:
            ids = list(dict.fromkeys(stream_ids))
            if not ids:
                return None
            query = query.filter(AcestreamChannel.id.in_(ids))
        rows = {row.id: row for row in query.all()}
        if not rows:
            return None
        plan = self.plan(root, rows.values())
        self.last_plan = plan

        if trigger != "preview":
            self._drop_expired_runs()
        run = ChannelTreeRun(trigger=trigger, stats={})
        self.db.add(run)
        self.db.flush()
        changes: list[ChannelTreeChange] = []

        def record(kind: str, *, stream_id=None, tv_channel_id=None, before=None, after=None) -> None:
            changes.append(ChannelTreeChange(run_id=run.id, kind=kind, stream_id=stream_id,
                                             tv_channel_id=tv_channel_id, before=before, after=after))

        created: dict[str, TVChannel] = {}
        channels: dict[int, TVChannel] = {}

        def channel_for(name: str) -> Optional[TVChannel]:
            cid = plan.targets.get(name)
            if cid is not None:
                if cid not in channels:
                    channels[cid] = self.db.get(TVChannel, cid)
                return channels[cid]
            if name not in plan.targets:
                return None  # ambiguous
            key = tree_engine.norm(name)
            if key not in created:
                tv = TVChannel(name=plan.new_names[key], is_active=True)
                self.db.add(tv)
                self.db.flush()
                created[key] = tv
                channels[tv.id] = tv
                record("create", tv_channel_id=tv.id, after={"name": tv.name})
            return created[key]

        stats = Counter()
        reached: dict[int, list[Placement]] = defaultdict(list)
        for p in plan.placements:
            row = rows[p.stream_id]
            stats[p.status] += 1
            if p.status == "rest":
                continue
            if p.status == "channel" and p.channel in plan.ambiguous:
                stats["ambiguous"] += 1
                continue
            tv = channel_for(p.channel) if p.status == "channel" else None
            target = tv.id if tv is not None else None
            if tv is not None:
                reached[tv.id].append(p)
            if row.tv_channel_id != target:
                record("assign", stream_id=row.id, tv_channel_id=target,
                       before={"tv_channel_id": row.tv_channel_id}, after={"tv_channel_id": target})
                row.tv_channel_id = target
                row.stream_order = None  # its place belonged to the old channel
                stats["moved"] += 1
        self.db.flush()

        guides = self._known_guides()
        warnings: list[dict[str, Any]] = []
        for cid, placed in reached.items():
            tv = channels[cid]
            self._add_groups(tv, placed, record)
            self._set_guide(tv, placed, guides, record, warnings)

        if not changes and trigger == "auto":
            # Streams the tree leaves without a channel come back on every import: no empty runs.
            # A manual run is kept, changes or not: its counts and warnings are the answer.
            self.db.delete(run)
            self.db.flush()
            return None
        self.db.add_all(changes)
        stats.update({"created": len(created), "changes": len(changes)})
        run.stats = {**dict(stats), "ambiguous_names": sorted(plan.ambiguous)[:50], "warnings": warnings[:100]}
        self.db.flush()
        return run

    def _drop_expired_runs(self) -> None:
        """Runs past the undo window can no longer be undone: their record goes."""
        cutoff = _utcnow() - timedelta(days=UNDO_DAYS)
        for old in self.db.query(ChannelTreeRun).filter(ChannelTreeRun.created_at < cutoff).all():
            self.db.delete(old)

    def _add_groups(self, tv: TVChannel, placed: list[Placement], record) -> None:
        current = split_groups(tv.category)
        wanted = list(current)
        for p in placed:
            for group in p.groups:
                if group not in wanted:
                    wanted.append(group)
        if wanted != current:
            before = tv.category
            tv.category = "; ".join(wanted)
            record("category", tv_channel_id=tv.id, before={"category": before}, after={"category": tv.category})

    def _known_guides(self) -> dict[str, int]:
        known: dict[str, int] = {}
        for xml_id, source_id in (self.db.query(EPGChannel.channel_xml_id, EPGChannel.epg_source_id)
                                  .order_by(EPGChannel.id).all()):
            known.setdefault(xml_id, source_id)
        return known

    def _set_guide(self, tv: TVChannel, placed: list[Placement], guides: dict[str, int], record,
                   warnings: list[dict[str, Any]]) -> None:
        written = {p.guide for p in placed if p.guide != ""}
        if len(written) > 1:
            warnings.append({"channel": tv.name, "kind": "leaf_guides_disagree"})
            return
        if written:
            guide = written.pop()
            if guide is None:
                self._write_guide(tv, None, None, record)
            elif guide in guides:
                self._write_guide(tv, guide, guides[guide], record)
            else:
                warnings.append({"channel": tv.name, "kind": "leaf_guide_not_loaded", "guide": guide})
            return
        votes = Counter(tvg for (tvg,) in self.db.query(AcestreamChannel.tvg_id)
                        .filter(AcestreamChannel.tv_channel_id == tv.id).all() if tvg)
        if not votes:
            return
        ranked = votes.most_common(2)
        top, count = ranked[0]
        if count < MIN_GUIDE_VOTES or (len(ranked) > 1 and ranked[1][1] == count) or top not in guides:
            return
        if tv.epg_id:
            if tv.epg_id != top:
                warnings.append({"channel": tv.name, "kind": "majority_differs", "guide": top})
            return
        self._write_guide(tv, top, guides[top], record)

    @staticmethod
    def _write_guide(tv: TVChannel, epg_id: Optional[str], source_id: Optional[int], record) -> None:
        if tv.epg_id == epg_id and (epg_id is None or tv.epg_source_id == source_id):
            return
        record("guide", tv_channel_id=tv.id, before={"epg_id": tv.epg_id, "epg_source_id": tv.epg_source_id},
               after={"epg_id": epg_id, "epg_source_id": source_id})
        tv.epg_id, tv.epg_source_id = epg_id, source_id

    def stream_ids(self, scope: str) -> Optional[list[str]]:
        """``all`` (None: every stream) or ``unassigned`` (streams without a channel)."""
        if scope == "all":
            return None
        if scope == "unassigned":
            return [sid for (sid,) in self.db.query(AcestreamChannel.id)
                    .filter(AcestreamChannel.tv_channel_id.is_(None)).all()]
        raise TreeRunError("scope is «all» or «unassigned»")

    def preview(self, scope: str = "all", root: Optional[dict[str, Any]] = None,
                limit: int = 500) -> Optional[dict[str, Any]]:
        """What ``apply`` would do, computed by applying inside a savepoint and rolling it back.

        Each change says why: the stream's name, the channel it leaves, the path through the
        tree (node labels) and how the channel was found. Streams left without a channel are
        listed with where they stopped, and every node counts the streams that reached it.
        Lists are cut at ``limit`` and say so.
        """
        root = root if root is not None else self.tree()
        ids = self.stream_ids(scope)
        nested = self.db.begin_nested()
        try:
            run = self.apply(ids, trigger="preview", root=root)
            if run is None:
                return None
            plan = self.last_plan
            labels = _labels(root)
            placed = {p.stream_id: p for p in plan.placements}
            stream_names: dict[str, str] = {}
            wanted = list(placed)
            for offset in range(0, len(wanted), 500):
                stream_names.update(self.db.query(AcestreamChannel.id, AcestreamChannel.name)
                                    .filter(AcestreamChannel.id.in_(wanted[offset:offset + 500])).all())
            names = {tv.id: tv.name for tv in self.db.query(TVChannel.id, TVChannel.name).all()}
            query = self.db.query(ChannelTreeChange).filter(ChannelTreeChange.run_id == run.id)
            total = query.count()
            changes = []
            for c in query.order_by(ChannelTreeChange.id).limit(limit).all():
                p = placed.get(c.stream_id)
                before_id = (c.before or {}).get("tv_channel_id")
                changes.append({"kind": c.kind, "stream_id": c.stream_id, "tv_channel_id": c.tv_channel_id,
                                "channel": names.get(c.tv_channel_id), "before": c.before, "after": c.after,
                                "stream_name": stream_names.get(c.stream_id),
                                "before_channel": names.get(before_id) if before_id is not None else None,
                                "path": [labels.get(n, n) for n in p.path[1:]] if p else [],
                                "how": p.how if p else ""})
            unplaced_all = [p for p in plan.placements
                            if p.status in ("rest", "none") or p.how == "ambiguous"]
            unplaced = [{"stream_id": p.stream_id, "stream_name": stream_names.get(p.stream_id),
                         "status": "ambiguous" if p.how == "ambiguous" else p.status,
                         "channel": p.channel, "candidates": plan.ambiguous.get(p.channel, []) if p.channel else [],
                         "path": [labels.get(n, n) for n in p.path[1:]]}
                        for p in unplaced_all[:limit]]
            reached = Counter(n for p in plan.placements for n in p.path)
            nodes = [{"id": n, "label": labels.get(n, n), "streams": reached[n]} for n in labels if reached[n]]
            return {"stats": dict(run.stats), "changes": changes, "total_changes": total,
                    "truncated": total > len(changes) or len(unplaced_all) > len(unplaced),
                    "unplaced": unplaced, "unplaced_total": len(unplaced_all), "nodes": nodes}
        finally:
            nested.rollback()

    def explain(self, stream_id: str, root: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        """Where the tree sends one stream and why. Reads only."""
        root = root if root is not None else self.tree()
        row = self.db.get(AcestreamChannel, stream_id)
        if root is None or row is None:
            return None
        p = self.plan(root, [row]).placements[0]
        labels = _labels(root)
        current = self.db.get(TVChannel, row.tv_channel_id) if row.tv_channel_id is not None else None
        return {"stream_id": row.id, "stream_name": row.name, "status": p.status, "channel": p.channel,
                "how": p.how, "path": [labels.get(n, n) for n in p.path[1:]], "groups": p.groups,
                "guide": p.guide, "current_channel": current.name if current else None}

    # ------------------------------------------------------------ undo
    def runs(self, limit: int = 20) -> list[ChannelTreeRun]:
        return self.db.query(ChannelTreeRun).order_by(ChannelTreeRun.id.desc()).limit(limit).all()

    def _later_runs_touching(self, run: ChannelTreeRun) -> list[int]:
        """Later runs, not undone, that changed a stream or channel this run changed."""
        streams = {c.stream_id for c in run.changes if c.stream_id is not None}
        channels = {c.tv_channel_id for c in run.changes if c.tv_channel_id is not None}
        if not streams and not channels:
            return []
        query = (self.db.query(ChannelTreeChange.run_id).join(ChannelTreeRun)
                 .filter(ChannelTreeRun.id > run.id, ChannelTreeRun.undone_at.is_(None),
                         ChannelTreeRun.trigger != "preview"))
        touching = set()
        for column, values in ((ChannelTreeChange.stream_id, streams), (ChannelTreeChange.tv_channel_id, channels)):
            values = list(values)
            for offset in range(0, len(values), 500):
                touching.update(rid for (rid,) in query.filter(column.in_(values[offset:offset + 500])).distinct())
        return sorted(touching)

    def undo(self, run_id: int) -> dict[str, int]:
        """Put back what run ``run_id`` replaced. Flushes, does not commit."""
        run = self.db.get(ChannelTreeRun, run_id)
        if run is None:
            raise TreeRunNotFound("no such run")
        if run.undone_at is not None:
            raise TreeRunError("this run was already undone")
        if _utcnow() - run.created_at > timedelta(days=UNDO_DAYS):
            raise TreeRunError(f"runs can be undone for {UNDO_DAYS} days")
        later = self._later_runs_touching(run)
        if later:
            raise TreeRunError("run " + ", ".join(map(str, later))
                               + " changed the same streams or channels later; undo it first")
        result = Counter()
        created = []
        for change in reversed(run.changes):
            before = change.before or {}
            if change.kind == "assign":
                row = self.db.get(AcestreamChannel, change.stream_id)
                if row is None:
                    continue
                previous = before.get("tv_channel_id")
                if previous is not None and self.db.get(TVChannel, previous) is None:
                    previous = None
                row.tv_channel_id = previous
                row.stream_order = None
                result["streams"] += 1
                continue
            tv = self.db.get(TVChannel, change.tv_channel_id) if change.tv_channel_id is not None else None
            if tv is None:
                continue
            if change.kind == "category":
                tv.category = before.get("category")
            elif change.kind == "guide":
                tv.epg_id, tv.epg_source_id = before.get("epg_id"), before.get("epg_source_id")
            elif change.kind == "create" and tv.name == (change.after or {}).get("name"):
                created.append(tv)  # same id and name: not a later channel that got the id again
        self.db.flush()
        for tv in created:
            in_use = self.db.query(AcestreamChannel.id).filter(AcestreamChannel.tv_channel_id == tv.id).first()
            if in_use is None:
                # As a normal delete does: a saved recording must not follow the id to another channel.
                self.db.query(Recording).filter_by(tv_channel_id=tv.id).update(
                    {"tv_channel_id": None, "revision": Recording.revision + 1}, synchronize_session="fetch")
                self.db.delete(tv)
                result["channels_removed"] += 1
            else:
                result["channels_kept"] += 1
        run.undone_at = _utcnow()
        self.db.flush()
        return dict(result)
