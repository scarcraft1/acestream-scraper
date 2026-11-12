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


class ChannelTreeChangeOut(BaseModel):
    kind: str
    stream_id: Optional[str] = None
    tv_channel_id: Optional[int] = None
    channel: Optional[str] = None
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None


class ChannelTreePreview(BaseModel):
    stats: dict[str, Any]
    changes: list[ChannelTreeChangeOut]


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
