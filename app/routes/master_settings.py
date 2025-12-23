"""
Master Settings API Routes
Single source of truth for all user parameters
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Any, Dict

from app.middleware.auth import get_current_user
from app.database import Database
from app.engines.master_settings_service import create_master_settings_service


router = APIRouter(prefix="/master-settings", tags=["master-settings"])


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
        raise HTTPException(status_code=500, detail=str(e))


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
        return result
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))  # Conflict
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))
