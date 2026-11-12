"""DTOs for the channel rules tree: the stored tree, previews, runs and undo."""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

TreeScope = Literal["all", "unassigned"]


class ChannelTreeState(BaseModel):
    tree: Optional[dict[str, Any]] = Field(None, description="The stored rules tree, or null when there is none")
    auto: bool = Field(False, description="Imports put new streams without a channel through the tree")


class ChannelTreeUpdate(BaseModel):
    tree: Optional[dict[str, Any]] = Field(None, description="Rules tree to store; omitted keeps the stored one")
    auto: Optional[bool] = Field(None, description="Switch for imports; omitted keeps it")


class ChannelTreeScope(BaseModel):
    scope: TreeScope = Field("all", description="all: every stream; unassigned: streams without a channel")


MatchKind = Literal["", "existing", "publisher_code", "new", "ambiguous"]


class ChannelTreeChangeOut(BaseModel):
    kind: str
    stream_id: Optional[str] = None
    tv_channel_id: Optional[int] = None
    channel: Optional[str] = None
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None
    stream_name: Optional[str] = Field(None, description="Name of the stream the change moves")
    before_channel: Optional[str] = Field(None, description="Channel the stream leaves")
    path: list[str] = Field(default_factory=list, description="Tree nodes the stream went through, below the root")
    how: MatchKind = Field("", description="How the leaf's channel was found")


class ChannelTreeUnplacedOut(BaseModel):
    stream_id: str
    stream_name: Optional[str] = None
    status: Literal["rest", "none", "ambiguous"] = Field(
        ..., description="rest: no child matched; none: a leaf without channel; ambiguous: two channels answer")
    channel: Optional[str] = None
    candidates: list[str] = Field(default_factory=list)
    path: list[str] = Field(default_factory=list, description="Where the stream stopped")


class ChannelTreeNodeCount(BaseModel):
    id: str
    label: str
    streams: int


class ChannelTreePreview(BaseModel):
    stats: dict[str, Any]
    changes: list[ChannelTreeChangeOut]
    total_changes: int = 0
    truncated: bool = Field(False, description="A list was cut at the limit")
    unplaced: list[ChannelTreeUnplacedOut] = Field(default_factory=list)
    unplaced_total: int = 0
    nodes: list[ChannelTreeNodeCount] = Field(default_factory=list, description="Streams that reached each node")


class ChannelTreeExplainIn(BaseModel):
    stream_id: str


class ChannelTreeExplain(BaseModel):
    stream_id: str
    stream_name: Optional[str] = None
    status: Literal["channel", "none", "rest"]
    channel: Optional[str] = None
    how: MatchKind = ""
    path: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    guide: Optional[str] = Field("", description="null: no guide; empty: automatic")
    current_channel: Optional[str] = None


class ChannelTreeRunOut(BaseModel):
    id: int
    created_at: datetime
    trigger: str
    stats: dict[str, Any]
    undone_at: Optional[datetime] = None


class ChannelTreeUndoOut(BaseModel):
    streams: int = 0
    channels_removed: int = 0
    channels_kept: int = 0
