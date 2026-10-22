"""
Watchman Cycles Routes
Endpoints for managing work rotation cycles
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date

from app.database import Database
from app.middleware.auth import get_current_user
from app.engines.calendar_engine import create_calendar_engine, generation_bounds
from app.engines.command_executor import create_command_executor
from app.engines.master_settings_service import MasterSettingsService
from app.engines.calendar_authority import (
    engine_cycle,
    pattern_length,
    projection_for_user,
    remember_cycle,
    sync_cycle_row,
)
from app.onboarding import finish_onboarding_for_saved_cycle
from loguru import logger


router = APIRouter()


def _settings_cycle_follows(current_cycle, cycle_id: str) -> bool:
    """A row may update the document cycle when that cycle is empty or the same id."""
    if not isinstance(current_cycle, dict) or not current_cycle.get("id"):
        return True
    return str(current_cycle.get("id")) == str(cycle_id)


def _overlay_cycle_patch(current_cycle, update_data: dict, cycle_id: str) -> dict:
    """Settings cycle plus only the fields this PATCH sent."""
    merged = dict(current_cycle) if isinstance(current_cycle, dict) else {}
    if "name" in update_data:
        merged["name"] = update_data["name"]
    if "pattern" in update_data:
        merged["pattern"] = update_data["pattern"]
    if "cycle_length" in update_data:
        merged["total_days"] = update_data["cycle_length"]
    elif "pattern" in update_data:
        derived = pattern_length(update_data["pattern"])
        if derived is not None:
            merged["total_days"] = derived
    if "anchor_date" in update_data:
        merged["anchor_date"] = update_data["anchor_date"]
    if "anchor_cycle_day" in update_data:
        merged["anchor_cycle_day"] = update_data["anchor_cycle_day"]
    if not merged.get("id"):
        merged["id"] = cycle_id
    return merged


class CycleBlockSchema(BaseModel):
    label: str = Field(..., description="work_day, work_night, or off")
    duration: int = Field(..., ge=0, description="Duration in days")


class CreateCycleRequest(BaseModel):
    name: str = "Default Rotation"
    pattern: List[CycleBlockSchema]
    anchor_date: date
    anchor_cycle_day: int = Field(..., ge=1)
    crew: Optional[str] = None
    description: Optional[str] = None


class UpdateCycleRequest(BaseModel):
    name: Optional[str] = None
    pattern: Optional[List[CycleBlockSchema]] = None
    anchor_date: Optional[date] = None
    anchor_cycle_day: Optional[int] = None
    is_active: Optional[bool] = None
    crew: Optional[str] = None
    description: Optional[str] = None


@router.get("")
async def list_cycles(user: dict = Depends(get_current_user)):
    """Get all cycles for the current user"""
    logger.info(f"[CYCLES] GET /cycles - user_id: {user['id']}")
    db = Database(use_admin=True)
    cycles = await db.get_cycles(user["id"])
    logger.info(f"[CYCLES] Found {len(cycles)} cycles for user {user['id']}")

    return {
        "success": True,
        "data": cycles
    }


@router.get("/active")
async def get_active_cycle(user: dict = Depends(get_current_user)):
    """Get the currently active cycle"""
    logger.info(f"[CYCLES] GET /cycles/active - user_id: {user['id']}")
    db = Database(use_admin=True)
    cycle = await db.get_active_cycle(user["id"])

    if not cycle:
        logger.info(f"[CYCLES] No active cycle found for user {user['id']}")
        return {
            "success": True,
            "data": None,
            "message": "No active cycle found"
        }

    logger.info(f"[CYCLES] Active cycle found: {cycle.get('id')} - {cycle.get('name')}")
    return {
        "success": True,
        "data": cycle
    }


@router.post("")
async def create_cycle(
    data: CreateCycleRequest,
    user: dict = Depends(get_current_user)
):
    """Create a new cycle and auto-generate calendar"""
    db = Database(use_admin=True)

    logger.info(f"User {user['id']} creating cycle: {data.name}")
    
    # Check tier limits for free users
    tier = user.get("tier", "free")
    if tier == "free":
        existing_cycles = await db.get_cycles(user["id"])
        if len(existing_cycles) >= 1:
            logger.warning(f"Free tier user {user['id']} blocked from creating additional cycle")
            raise HTTPException(
                status_code=403,
                detail="You've reached the free plan limit of 1 rotation cycle. Ready for more flexibility? Upgrade to Pro for unlimited rotations."
            )
    
    # Calculate cycle length
    cycle_length = sum(block.duration for block in data.pattern)
    
    # Validate
    if data.anchor_cycle_day > cycle_length:
        raise HTTPException(
            status_code=400,
            detail=f"anchor_cycle_day ({data.anchor_cycle_day}) cannot exceed cycle length ({cycle_length})"
        )
    
    # Prepare cycle data
    cycle_data = {
        "user_id": user["id"],
        "name": data.name,
        "pattern": [{"label": b.label, "duration": b.duration} for b in data.pattern],
        "cycle_length": cycle_length,
        "anchor_date": data.anchor_date.isoformat(),
        "anchor_cycle_day": data.anchor_cycle_day,
        "crew": data.crew,
        "description": data.description,
        "is_active": True
    }
    
    # Deactivate other cycles first
    existing_cycles = await db.get_cycles(user["id"])
    for existing in existing_cycles:
        if existing.get("is_active"):
            await db.update_cycle(existing["id"], {"is_active": False}, user_id=user["id"])
    
    # Create the new cycle
    cycle = await db.create_cycle(cycle_data)

    # Creating a rotation is the user's choice of the active cycle, so the
    # settings copy is replaced even when it still tracks another id.
    await remember_cycle(db, user["id"], cycle, replace=True)
    projection = await projection_for_user(db, user["id"])
    source = projection.get("cycle") or cycle
    prepared = engine_cycle(source)
    
    # AUTO-GENERATE CALENDAR from the Master Settings cycle
    from datetime import date as date_module
    anchor_value = prepared.get("anchor_date") or data.anchor_date
    anchor_date = anchor_value if not isinstance(anchor_value, str) else date_module.fromisoformat(anchor_value[:10])
    # 1 Jan of the anchor year through the end of a future year. Days before
    # the anchor are the rotation projected backwards.
    start_date, end_date = generation_bounds(anchor_date)
    
    logger.info(f"Auto-generating calendar from {start_date} to {end_date}")
    
    try:
        executor = create_command_executor(db, user["id"])
        await executor.rebuild_calendar(start_date, end_date)
        logger.info(f"Generated calendar days from {start_date} to {end_date}")
    except Exception as e:
        logger.error(f"Failed to auto-generate calendar: {e}")
        # Don't fail the cycle creation, just log the error

    await finish_onboarding_for_saved_cycle(db, user["id"])
    
    return {
        "success": True,
        "message": f"Cycle created and calendar generated from {start_date}",
        "data": cycle
    }


@router.patch("/{cycle_id}")
async def update_cycle(
    cycle_id: str,
    data: UpdateCycleRequest,
    user: dict = Depends(get_current_user)
):
    """Update an existing cycle and regenerate calendar if needed"""
    db = Database(use_admin=True)

    # Confirm ownership before any side effects, including deactivating other cycles.
    owned_cycles = await db.get_cycles(user["id"])
    if not any(c.get("id") == cycle_id for c in owned_cycles):
        raise HTTPException(status_code=404, detail="Cycle not found")
    
    # Track if we need to regenerate calendar
    needs_regeneration = False
    
    # Build update data
    update_data = {}
    
    if data.name is not None:
        update_data["name"] = data.name
    
    if data.pattern is not None:
        update_data["pattern"] = [{"label": b.label, "duration": b.duration} for b in data.pattern]
        update_data["cycle_length"] = sum(b.duration for b in data.pattern)
        needs_regeneration = True
    
    if data.anchor_date is not None:
        update_data["anchor_date"] = data.anchor_date.isoformat()
        needs_regeneration = True
    
    if data.anchor_cycle_day is not None:
        update_data["anchor_cycle_day"] = data.anchor_cycle_day
        needs_regeneration = True
    
    if data.is_active is not None:
        update_data["is_active"] = data.is_active
        
        # If activating this cycle, deactivate others
        if data.is_active:
            existing_cycles = await db.get_cycles(user["id"])
            for existing in existing_cycles:
                if existing.get("id") != cycle_id and existing.get("is_active"):
                    await db.update_cycle(existing["id"], {"is_active": False}, user_id=user["id"])
            needs_regeneration = True
    
    if data.crew is not None:
        update_data["crew"] = data.crew
    
    if data.description is not None:
        update_data["description"] = data.description
    
    if not update_data:
        return {"message": "No changes provided"}
    
    cycle = await db.update_cycle(cycle_id, update_data, user_id=user["id"])
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")

    if update_data.get("is_active") is True:
        # This row is now the active rotation, so it replaces the settings
        # cycle. Other active rows were already deactivated above.
        await remember_cycle(db, user["id"], {**cycle, **update_data}, replace=True)
    else:
        # Start from the settings copy and lay only the fields this request sent
        # on top. Copying the table row would put a stale anchor back. A settings
        # failure leaves the row update in place.
        try:
            service = MasterSettingsService(db)
            current = await service.get(user["id"])
            current_cycle = (current.get("settings") or {}).get("cycle") if isinstance(current, dict) else None
            if _settings_cycle_follows(current_cycle, cycle_id):
                merged = _overlay_cycle_patch(current_cycle, update_data, cycle_id)
                await service.update_section(user["id"], "cycle", merged)
                await sync_cycle_row(db, user["id"], merged)
        except Exception as exc:
            logger.warning(f"Could not mirror cycle {cycle_id} into settings for {user['id']}: {exc}")
    await finish_onboarding_for_saved_cycle(db, user["id"])
    
    # AUTO-REGENERATE CALENDAR if pattern/anchor changed.
    # Regeneration reads Master Settings, not the relational row, so a different
    # saved cycle cannot be overwritten by this update.
    if needs_regeneration and cycle:
        projection = await projection_for_user(db, user["id"])
        prepared = engine_cycle(projection.get("cycle") or cycle)
        anchor_date_str = prepared.get("anchor_date")
        if anchor_date_str:
            from datetime import date as date_module
            anchor_date = date_module.fromisoformat(str(anchor_date_str)[:10])
            start_date, end_date = generation_bounds(anchor_date)
            
            logger.info(f"Regenerating calendar from {start_date} to {end_date} after cycle update")
            
            try:
                executor = create_command_executor(db, user["id"])
                await executor.rebuild_calendar(start_date, end_date)
                logger.info(f"Regenerated calendar from {start_date} to {end_date}")
            except Exception as e:
                logger.error(f"Failed to regenerate calendar: {e}")
    
    return {
        "success": True,
        "message": "Cycle updated" + (" and calendar regenerated" if needs_regeneration else ""),
        "data": cycle
    }


@router.delete("/{cycle_id}")
async def delete_cycle(
    cycle_id: str,
    user: dict = Depends(get_current_user)
):
    """Delete a cycle"""
    db = Database(use_admin=True)
    deleted = await db.delete_cycle(cycle_id, user_id=user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Cycle not found")
    
    return {
        "success": True,
        "message": "Cycle deleted"
    }


@router.post("/{cycle_id}/preview")
async def preview_cycle(
    cycle_id: str,
    year: int = 2026,
    user: dict = Depends(get_current_user)
):
    """Preview what a year would look like with this cycle"""
    db = Database(use_admin=True)
    
    # Get the cycle
    cycles = await db.get_cycles(user["id"])
    cycle = next((c for c in cycles if c["id"] == cycle_id), None)
    
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    
    # Get leave blocks
    leave_blocks = await db.get_leave_blocks(user["id"])
    
    # Generate preview
    engine = create_calendar_engine(user["id"])
    days = engine.generate_year(year, cycle, leave_blocks)
    
    # Convert to dictionaries
    days_data = [
        {
            "date": d.date.isoformat(),
            "cycle_day": d.cycle_day,
            "work_type": d.work_type.value,
            "state_json": d.state_json
        }
        for d in days
    ]
    
    return {
        "success": True,
        "data": {
            "cycle": cycle,
            "year": year,
            "total_days": len(days_data),
            "days": days_data
        }
    }
