"""
Master Settings API Routes
Single source of truth for all user parameters
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from typing import Optional, Any, Dict

from app.middleware.auth import get_current_user
from app.database import Database
from app.engines.calendar_authority import cycle_is_present, sync_cycle_row
from app.engines.master_settings_service import create_master_settings_service
from app.onboarding import finish_onboarding_for_saved_cycle


router = APIRouter(prefix="/master-settings", tags=["master-settings"])


def _saved_cycle(result, fallback) -> Optional[dict]:
    """The cycle stored by a successful settings write."""
    settings = result.get("settings") if isinstance(result, dict) else None
    saved = settings.get("cycle") if isinstance(settings, dict) else None
    if isinstance(saved, dict):
        return saved
    return fallback if isinstance(fallback, dict) else None


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
    db = Database(use_admin=True)
    service = create_master_settings_service(db)
    
    try:
        result = await service.update(
            user["id"], 
            request.settings,
            request.expected_version
        )
        saved = _saved_cycle(result, (request.settings or {}).get("cycle"))
        if cycle_is_present(saved):
            await sync_cycle_row(db, user["id"], saved)
            await finish_onboarding_for_saved_cycle(db, user["id"])
        return result
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))  # Conflict
    except Exception as e:
        logger.exception(f"[SETTINGS] Failed to update master settings for {user['id']}: {e}")
        raise HTTPException(status_code=500, detail="Could not save your settings. Please try again.")


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
    
    db = Database(use_admin=True)
    service = create_master_settings_service(db)
    
    try:
        result = await service.update_section(user["id"], section, request.value)
        if section == "cycle":
            saved = _saved_cycle(result, request.value if isinstance(request.value, dict) else None)
            if cycle_is_present(saved):
                await sync_cycle_row(db, user["id"], saved)
                await finish_onboarding_for_saved_cycle(db, user["id"])
        return result
    except Exception as e:
        logger.exception(f"[SETTINGS] Failed to update {section} for {user['id']}: {e}")
        raise HTTPException(status_code=500, detail="Could not save your settings. Please try again.")


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
