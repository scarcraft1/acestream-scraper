"""Channel rules tree: store it, preview and apply it, list and undo its runs."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.error_handlers import APIError
from app.config.database import get_db
from app.repositories.settings_repository import SettingsRepository
from app.schemas.channel_tree import (ChannelTreePreview, ChannelTreeRunOut, ChannelTreeScope, ChannelTreeState,
                                      ChannelTreeUndoOut, ChannelTreeUpdate)
from app.services.channel_tree_service import AUTO_SETTING, ChannelTreeService, TreeRunError

router = APIRouter()


def _state(service: ChannelTreeService) -> ChannelTreeState:
    return ChannelTreeState(tree=service.tree(), auto=service.auto_enabled())


@router.get("", response_model=ChannelTreeState)
def get_tree(db: Session = Depends(get_db)) -> ChannelTreeState:
    return _state(ChannelTreeService(db))


@router.put("", response_model=ChannelTreeState)
def put_tree(body: ChannelTreeUpdate, db: Session = Depends(get_db)) -> ChannelTreeState:
    service = ChannelTreeService(db)
    try:
        if body.tree is not None:
            service.save_tree(body.tree)
    except TreeRunError as exc:
        raise APIError("CHANNEL_TREE_INVALID", str(exc), 422) from exc
    if body.auto is not None:
        SettingsRepository(db).set_setting(AUTO_SETTING, "true" if body.auto else "false")
    return _state(service)


def _require_tree(service: ChannelTreeService) -> None:
    if service.tree() is None:
        raise APIError("CHANNEL_TREE_MISSING", "No rules tree is stored.", 409)


@router.post("/preview", response_model=ChannelTreePreview)
def preview(body: ChannelTreeScope, db: Session = Depends(get_db)) -> ChannelTreePreview:
    service = ChannelTreeService(db)
    _require_tree(service)
    result = service.preview(body.scope)
    return ChannelTreePreview(**(result or {"stats": {}, "changes": []}))


@router.post("/apply", response_model=ChannelTreeRunOut)
def apply(body: ChannelTreeScope, db: Session = Depends(get_db)) -> ChannelTreeRunOut:
    service = ChannelTreeService(db)
    _require_tree(service)
    run = service.apply(service.stream_ids(body.scope), trigger="manual")
    if run is None:
        raise APIError("CHANNEL_TREE_NOTHING", "There are no streams to apply the tree to.", 409)
    db.commit()
    return ChannelTreeRunOut.model_validate(run, from_attributes=True)


@router.get("/runs", response_model=list[ChannelTreeRunOut])
def runs(db: Session = Depends(get_db)) -> list[ChannelTreeRunOut]:
    return [ChannelTreeRunOut.model_validate(r, from_attributes=True) for r in ChannelTreeService(db).runs()]


@router.post("/runs/{run_id}/undo", response_model=ChannelTreeUndoOut)
def undo(run_id: int, db: Session = Depends(get_db)) -> ChannelTreeUndoOut:
    try:
        result = ChannelTreeService(db).undo(run_id)
    except TreeRunError as exc:
        db.rollback()
        raise APIError("CHANNEL_TREE_UNDO", str(exc), 409) from exc
    db.commit()
    return ChannelTreeUndoOut(**result)
