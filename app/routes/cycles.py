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
    db = Database(use_admin=True)
    cycles = await db.get_cycles(user["id"])
    
    return {
        "success": True,
        "data": cycles
    }


@router.get("/active")
async def get_active_cycle(user: dict = Depends(get_current_user)):
    """Get the currently active cycle"""
    db = Database(use_admin=True)
    cycle = await db.get_active_cycle(user["id"])
    
    if not cycle:
        return {
            "success": True,
            "data": None,
            "message": "No active cycle found"
        }
    
    return {
        "success": True,
        "data": cycle
    }


@router.post("")
async def create_cycle(
    data: CreateCycleRequest,
    user: dict = Depends(get_current_user)
):
    """Create a new cycle"""
    db = Database(use_admin=True)
    # Engine reserved for future calendar auto-generation
    _engine = create_calendar_engine(user["id"])  # noqa: F841
    
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
    
    return {
        "success": True,
        "message": "Cycle created successfully",
        "data": cycle
    }


@router.patch("/{cycle_id}")
async def update_cycle(
    cycle_id: str,
    data: UpdateCycleRequest,
    user: dict = Depends(get_current_user)
):
    """Update an existing cycle"""
    db = Database(use_admin=True)
    
    # Build update data
    update_data = {}
    
    if data.name is not None:
        update_data["name"] = data.name
    
    if data.pattern is not None:
        update_data["pattern"] = [{"label": b.label, "duration": b.duration} for b in data.pattern]
        update_data["cycle_length"] = sum(b.duration for b in data.pattern)
    
    if data.anchor_date is not None:
        update_data["anchor_date"] = data.anchor_date.isoformat()
    
    if data.anchor_cycle_day is not None:
        update_data["anchor_cycle_day"] = data.anchor_cycle_day
    
    if data.is_active is not None:
        update_data["is_active"] = data.is_active
        
        # If activating this cycle, deactivate others
        if data.is_active:
            existing_cycles = await db.get_cycles(user["id"])
            for existing in existing_cycles:
                if existing.get("id") != cycle_id and existing.get("is_active"):
                    await db.update_cycle(existing["id"], {"is_active": False})
    
    if data.crew is not None:
        update_data["crew"] = data.crew
    
    if data.description is not None:
        update_data["description"] = data.description
    
    if not update_data:
        return {"message": "No changes provided"}
    
    cycle = await db.update_cycle(cycle_id, update_data)
    
    return {
        "success": True,
        "message": "Cycle updated",
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
