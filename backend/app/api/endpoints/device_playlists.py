"""Device playlists: one M3U per device, each with its own groups and channels."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.error_handlers import APIError
from app.config.database import get_db
from app.models.models import DevicePlaylist
from app.schemas.device_playlist import (ChannelGroupOut, DevicePlaylistCreate, DevicePlaylistOut,
                                         DevicePlaylistUpdate)
from app.services import device_playlist_service as service

router = APIRouter()

MAX_PLAYLISTS = 100


def _out(db: Session, row: DevicePlaylist) -> DevicePlaylistOut:
    return DevicePlaylistOut.model_validate({**{c.name: getattr(row, c.name) for c in row.__table__.columns},
                                             **service.summary(db, row)})


def _get(db: Session, playlist_id: int) -> DevicePlaylist:
    try:
        return service.get_playlist(db, playlist_id)
    except service.DevicePlaylistNotFound:
        raise APIError("DEVICE_PLAYLIST_NOT_FOUND", "Device playlist not found.", 404) from None


@router.get("/groups", response_model=list[ChannelGroupOut])
def list_groups(db: Session = Depends(get_db)) -> list[ChannelGroupOut]:
    return [ChannelGroupOut(**g) for g in service.groups(db)]


@router.get("", response_model=list[DevicePlaylistOut])
def list_playlists(db: Session = Depends(get_db)) -> list[DevicePlaylistOut]:
    rows = db.query(DevicePlaylist).order_by(DevicePlaylist.owner_id, DevicePlaylist.id).all()
    return [_out(db, row) for row in rows]


@router.post("", response_model=DevicePlaylistOut, status_code=201)
def create_playlist(body: DevicePlaylistCreate, db: Session = Depends(get_db)) -> DevicePlaylistOut:
    if db.query(DevicePlaylist).count() >= MAX_PLAYLISTS:
        raise APIError("DEVICE_PLAYLIST_LIMIT", f"At most {MAX_PLAYLISTS} device playlists.", 409)
    row = DevicePlaylist(name=body.name.strip(), owner_id=body.owner_id or None,
                         disabled_groups=[], enabled_channels=[], disabled_channels=[])
    if body.copy_from is not None:
        source = _get(db, body.copy_from)
        service.save(db, row, disabled_groups=source.disabled_groups,
                     enabled_channels=source.enabled_channels, disabled_channels=source.disabled_channels)
    db.add(row)
    db.commit()
    return _out(db, row)


@router.get("/{playlist_id}", response_model=DevicePlaylistOut)
def get_playlist(playlist_id: int, db: Session = Depends(get_db)) -> DevicePlaylistOut:
    return _out(db, _get(db, playlist_id))


@router.put("/{playlist_id}", response_model=DevicePlaylistOut)
def update_playlist(playlist_id: int, body: DevicePlaylistUpdate, db: Session = Depends(get_db)) -> DevicePlaylistOut:
    row = _get(db, playlist_id)
    service.save(db, row, name=body.name, disabled_groups=body.disabled_groups,
                 enabled_channels=body.enabled_channels, disabled_channels=body.disabled_channels)
    db.commit()
    return _out(db, row)


@router.delete("/{playlist_id}", status_code=204)
def delete_playlist(playlist_id: int, db: Session = Depends(get_db)) -> None:
    db.delete(_get(db, playlist_id))
    db.commit()
