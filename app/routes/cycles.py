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
from app.engines.calendar_engine import create_calendar_engine
from app.engines.master_settings_service import MasterSettingsService
from loguru import logger


router = APIRouter()


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
    engine = create_calendar_engine(user["id"])
    
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
            await db.update_cycle(existing["id"], {"is_active": False})
    
    # Create the new cycle
    cycle = await db.create_cycle(cycle_data)
    
    # SYNC TO MASTER SETTINGS - critical for frontend to see the cycle
    try:
        ms_service = MasterSettingsService(db)
        cycle_for_settings = {
            "id": cycle["id"],
            "name": cycle["name"],
            "pattern": cycle["pattern"],
            "anchor_date": cycle["anchor_date"],
            "anchor_cycle_day": cycle["anchor_cycle_day"],
            "total_days": cycle["cycle_length"]
        }
        await ms_service.update_section(user["id"], "cycle", cycle_for_settings)
        logger.info(f"Synced cycle to master_settings for user {user['id']}")
    except Exception as e:
        logger.error(f"Failed to sync cycle to master_settings: {e}")
    
    # AUTO-GENERATE CALENDAR from anchor date forward only
    from datetime import date as date_module
    anchor_date = data.anchor_date
    start_date = anchor_date
    end_date = date_module(anchor_date.year, 12, 31)
    
    logger.info(f"Auto-generating calendar from {start_date} to {end_date}")
    
    try:
        leave_blocks = await db.get_leave_blocks(user["id"])
        days = engine.generate_range(start_date, end_date, cycle, leave_blocks)
        
        days_data = [
            {
                "user_id": user["id"],
                "date": d.date.isoformat(),
                "cycle_id": cycle["id"],
                "cycle_day": d.cycle_day,
                "work_type": d.work_type.value,
                "state_json": d.state_json
            }
            for d in days
        ]
        
        # Clear from anchor forward only
        await db.delete_calendar_days(user["id"], start_date.isoformat(), end_date.isoformat())
        await db.upsert_calendar_days(days_data)
        
        logger.info(f"Generated {len(days_data)} calendar days from {start_date}")
    except Exception as e:
        logger.error(f"Failed to auto-generate calendar: {e}")
        # Don't fail the cycle creation, just log the error
    
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
    engine = create_calendar_engine(user["id"])
    
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
                    await db.update_cycle(existing["id"], {"is_active": False})
            needs_regeneration = True
    
    if data.crew is not None:
        update_data["crew"] = data.crew
    
    if data.description is not None:
        update_data["description"] = data.description
    
    if not update_data:
        return {"message": "No changes provided"}
    
    cycle = await db.update_cycle(cycle_id, update_data)
    
    # SYNC TO MASTER SETTINGS
    if cycle:
        try:
            ms_service = MasterSettingsService(db)
            cycle_for_settings = {
                "id": cycle["id"],
                "name": cycle["name"],
                "pattern": cycle["pattern"],
                "anchor_date": cycle["anchor_date"],
                "anchor_cycle_day": cycle["anchor_cycle_day"],
                "total_days": cycle["cycle_length"]
            }
            await ms_service.update_section(user["id"], "cycle", cycle_for_settings)
            logger.info(f"Synced updated cycle to master_settings for user {user['id']}")
        except Exception as e:
            logger.error(f"Failed to sync cycle to master_settings: {e}")
    
    # AUTO-REGENERATE CALENDAR if pattern/anchor changed
    if needs_regeneration and cycle:
        anchor_date_str = cycle.get("anchor_date")
        if anchor_date_str:
            from datetime import date as date_module
            anchor_date = date_module.fromisoformat(anchor_date_str) if isinstance(anchor_date_str, str) else anchor_date_str
            
            # Generate from anchor date forward only
            start_date = anchor_date
            end_date = date_module(anchor_date.year, 12, 31)
            
            logger.info(f"Regenerating calendar from {start_date} to {end_date} after cycle update")
            
            try:
                leave_blocks = await db.get_leave_blocks(user["id"])
                days = engine.generate_range(start_date, end_date, cycle, leave_blocks)
                
                days_data = [
                    {
                        "user_id": user["id"],
                        "date": d.date.isoformat(),
                        "cycle_id": cycle["id"],
                        "cycle_day": d.cycle_day,
                        "work_type": d.work_type.value,
                        "state_json": d.state_json
                    }
                    for d in days
                ]
                
                # Delete from anchor forward, not entire year
                await db.delete_calendar_days(user["id"], start_date.isoformat(), end_date.isoformat())
                await db.upsert_calendar_days(days_data)
                
                logger.info(f"Regenerated {len(days_data)} calendar days from {start_date}")
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
    await db.delete_cycle(cycle_id)
    
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
