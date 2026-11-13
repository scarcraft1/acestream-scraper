"""DTOs for device playlists."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class DevicePlaylistCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    owner_id: Optional[str] = Field(None, max_length=64)
    # Start from another playlist's choices instead of everything enabled.
    copy_from: Optional[int] = None


class DevicePlaylistUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    disabled_groups: Optional[list[int]] = Field(None, max_length=5000)
    enabled_channels: Optional[list[int]] = Field(None, max_length=20000)
    disabled_channels: Optional[list[int]] = Field(None, max_length=20000)


class DevicePlaylistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    owner_id: Optional[str] = None
    disabled_groups: list[int]
    enabled_channels: list[int]
    disabled_channels: list[int]
    created_at: datetime
    updated_at: datetime
    channels: int = Field(description="TV channels the playlist keeps")
    total: int = Field(description="TV channels in the catalogue")
    tabs: int = Field(description="Distinct group-title tabs the playlist gives Kodi")


class ChannelGroupOut(BaseModel):
    id: int
    name: str
    parent_id: Optional[int] = None
    position: int
    channels: int
