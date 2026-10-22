"""
Master Settings API Routes
Single source of truth for all user parameters
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from typing import Optional, Any, Dict

from app.middleware.auth import get_current_user
from app.database import Database
from app.engines.calendar_authority import cycle_is_present, sync_cycle_row
from app.engines.master_settings_service import VersionConflictError, create_master_settings_service
from app.onboarding import finish_onboarding_for_saved_cycle
from app.routes.cycles import rebuild_for_user


router = APIRouter(prefix="/master-settings", tags=["master-settings"])


ANCHOR_REQUIRED = "Your rotation needs a start date before it can be saved."


def _has_anchor(cycle: dict) -> bool:
    anchor = cycle.get("anchor")
    if isinstance(anchor, dict) and anchor.get("date") not in (None, ""):
        return True
    return cycle.get("anchor_date") not in (None, "")


def _require_anchor(cycle) -> None:
    """A saved rotation object needs a start date. null means no rotation yet."""
    if isinstance(cycle, dict) and not _has_anchor(cycle):
        raise HTTPException(status_code=422, detail=ANCHOR_REQUIRED)


def _saved_cycle(result, fallback) -> Optional[dict]:
    """The cycle stored by a successful settings write."""
    settings = result.get("settings") if isinstance(result, dict) else None
    saved = settings.get("cycle") if isinstance(settings, dict) else None
    if isinstance(saved, dict):
        return saved
    return fallback if isinstance(fallback, dict) else None


def _cycle_changed(old, new) -> bool:
    """True when the saved document has a cycle and it is not the previous one."""
    if not cycle_is_present(new if isinstance(new, dict) else None):
        return False
    old_token = json.dumps(old, sort_keys=True, default=str) if isinstance(old, dict) else None
    new_token = json.dumps(new, sort_keys=True, default=str)
    return old_token != new_token


async def _stored_cycle(service, user_id: str):
    try:
        current = await service.get(user_id)
    except Exception as exc:
        logger.warning(f"Could not read settings before save for {user_id}: {exc}")
        return None
    if not isinstance(current, dict):
        return None
    return (current.get("settings") or {}).get("cycle")


async def _sync_changed_cycle(db, user_id: str, saved_cycle, old_cycle) -> None:
    if cycle_is_present(saved_cycle if isinstance(saved_cycle, dict) else None):
        try:
            await sync_cycle_row(db, user_id, saved_cycle, require_id=True)
        except Exception as exc:
            logger.warning(f"Could not sync cycles row after settings save for {user_id}: {exc}")
        await finish_onboarding_for_saved_cycle(db, user_id)
    if _cycle_changed(old_cycle, saved_cycle):
        await rebuild_for_user(db, user_id)


class UpdateSettingsRequest(BaseModel):
    settings: Dict[str, Any]
    expected_version: Optional[int] = None  # For optimistic locking


class UpdateSectionRequest(BaseModel):
    value: Any


@router.get("")
async def get_master_settings(
    user: dict = Depends(get_current_user)
):
    """
    Get the user's complete master settings.
    
    If no settings exist, creates default settings.
    """
    db = Database(use_admin=True)
    service = create_master_settings_service(db)
    
    try:
        result = await service.get(user["id"])
        return result
    except Exception as e:
        logger.exception(f"[SETTINGS] Failed to load master settings for {user['id']}: {e}")
        raise HTTPException(status_code=500, detail="Could not load your settings. Please try again.")


@router.put("")
async def update_master_settings(
    request: UpdateSettingsRequest,
    user: dict = Depends(get_current_user)
):
    """
    Update the entire master settings document.
    
    Use expected_version for optimistic locking to prevent conflicts.
    """
    _require_anchor((request.settings or {}).get("cycle"))
    db = Database(use_admin=True)
    service = create_master_settings_service(db)
    old_cycle = await _stored_cycle(service, user["id"])

    try:
        result = await service.update(
            user["id"],
            request.settings,
            request.expected_version
        )
    except VersionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as e:
        logger.exception(f"[SETTINGS] Failed to update master settings for {user['id']}: {e}")
        raise HTTPException(status_code=500, detail="Could not save your settings. Please try again.")

    saved = _saved_cycle(result, (request.settings or {}).get("cycle"))
    await _sync_changed_cycle(db, user["id"], saved, old_cycle)
    return result


@router.patch("/{section}")
async def update_section(
    section: str,
    request: UpdateSectionRequest,
    user: dict = Depends(get_current_user)
):
    """
    Update a specific section of master settings.
    
    Sections: cycle, work, constraints, commitments, leave_blocks, preferences
    """
    valid_sections = ["cycle", "work", "constraints", "commitments", "leave_blocks", "preferences"]
    
    if section not in valid_sections:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid section. Valid sections: {', '.join(valid_sections)}"
        )
    
    if section == "cycle":
        _require_anchor(request.value)
    db = Database(use_admin=True)
    service = create_master_settings_service(db)
    old_cycle = await _stored_cycle(service, user["id"]) if section == "cycle" else None

    try:
        result = await service.update_section(user["id"], section, request.value)
    except Exception as e:
        logger.exception(f"[SETTINGS] Failed to update {section} for {user['id']}: {e}")
        raise HTTPException(status_code=500, detail="Could not save your settings. Please try again.")

    if section == "cycle":
        saved = _saved_cycle(result, request.value if isinstance(request.value, dict) else None)
        await _sync_changed_cycle(db, user["id"], saved, old_cycle)
    return result


@router.get("/snapshot")
async def get_snapshot(
    user: dict = Depends(get_current_user)
):
    """
    Get a lightweight snapshot of current settings (just the settings object).
    
    Useful for passing to the agent as context.
    """
    db = Database(use_admin=True)
    service = create_master_settings_service(db)
    
    try:
        result = await service.get_snapshot(user["id"])
        return {"settings": result}
    except Exception as e:
        logger.exception(f"[SETTINGS] Failed to load snapshot for {user['id']}: {e}")
        raise HTTPException(status_code=500, detail="Could not load your settings. Please try again.")
