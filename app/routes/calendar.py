"""
Watchman Calendar Routes
Endpoints for calendar day management and generation
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime

from app.database import Database
from app.middleware.auth import get_current_user
from app.engines.calendar_engine import create_calendar_engine


from loguru import logger

router = APIRouter()


class GenerateCalendarRequest(BaseModel):
    year: int = 2026
    regenerate: bool = False


class LeaveBlockRequest(BaseModel):
    name: str = "Leave"
    start_date: date
    end_date: date
    notes: Optional[str] = None


@router.get("")
async def get_calendar_days(
    start_date: date = Query(...),
    end_date: date = Query(...),
    user: dict = Depends(get_current_user)
):
    """Get calendar days for a date range"""
    db = Database()
    
    days = await db.get_calendar_days(
        user["id"],
        start_date.isoformat(),
        end_date.isoformat()
    )
    
    return {
        "success": True,
        "data": days,
        "count": len(days)
    }


@router.get("/year/{year}")
async def get_year(
    year: int,
    user: dict = Depends(get_current_user)
):
    """Get all calendar days for a specific year"""
    db = Database()
    
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"
    
    days = await db.get_calendar_days(user["id"], start_date, end_date)
    
    return {
        "success": True,
        "data": days,
        "year": year,
        "count": len(days)
    }


@router.get("/month/{year}/{month}")
async def get_month(
    year: int,
    month: int,
    user: dict = Depends(get_current_user)
):
    """Get all calendar days for a specific month"""
    db = Database()
    
    # Calculate start and end dates
    start_date = f"{year}-{month:02d}-01"
    
    if month == 12:
        end_date = f"{year}-12-31"
    else:
        next_month = date(year, month + 1, 1)
        from datetime import timedelta
        last_day = next_month - timedelta(days=1)
        end_date = last_day.isoformat()
    
    days = await db.get_calendar_days(user["id"], start_date, end_date)
    
    return {
        "success": True,
        "data": days,
        "year": year,
        "month": month,
        "count": len(days)
    }


@router.get("/day/{date_str}")
async def get_day(
    date_str: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific calendar day with full details"""
    db = Database()
    
    day = await db.get_calendar_day(user["id"], date_str)
    
    if not day:
        return {
            "success": True,
            "data": None,
            "message": "No calendar data for this date"
        }
    
    return {
        "success": True,
        "data": day
    }


@router.post("/generate")
async def generate_calendar(
    data: GenerateCalendarRequest,
    user: dict = Depends(get_current_user)
):
    """Generate calendar days for a year based on active cycle"""
    db = Database()
    
    # Check tier limits for free users (6 months only)
    tier = user.get("tier", "free")
    if tier == "free":
        logger.info(f"Free tier user {user['id']} attempting calendar generation for {data.year}")
        # Free users can only plan 6 months ahead from today
        from datetime import date as date_module
        today = date_module.today()
        max_date = date_module(today.year, today.month + 6, 1) if today.month <= 6 else date_module(today.year + 1, today.month - 6, 1)
        
        if data.year > max_date.year:
            logger.warning(f"Free tier user {user['id']} blocked from generating {data.year} - exceeds 6 month limit")
            raise HTTPException(
                status_code=403,
                detail="The free plan covers 6 months of planning ahead. Want to see further into the future? Upgrade to Pro for unlimited years."
            )
    
    logger.info(f"User {user['id']} generating calendar for year {data.year}")
    
    # Get active cycle
    cycle = await db.get_active_cycle(user["id"])
    
    if not cycle:
        raise HTTPException(
            status_code=400,
            detail="No active cycle found. Please create a cycle first."
        )
    
    # Check if calendar already exists
    existing = await db.get_calendar_days(
        user["id"],
        f"{data.year}-01-01",
        f"{data.year}-12-31"
    )
    
    if existing and not data.regenerate:
        return {
            "success": True,
            "message": f"Calendar for {data.year} already exists. Set regenerate=true to overwrite.",
            "count": len(existing)
        }
    
    # Get leave blocks
    leave_blocks = await db.get_leave_blocks(user["id"])
    
    # Generate the year
    engine = create_calendar_engine(user["id"])
    days = engine.generate_year(data.year, cycle, leave_blocks)
    
    # Convert to dictionaries for database
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
    
    # Delete existing and insert new
    if existing:
        await db.delete_calendar_days(user["id"], f"{data.year}-01-01", f"{data.year}-12-31")
    
    # Upsert all days
    await db.upsert_calendar_days(days_data)
    
    return {
        "success": True,
        "message": f"Generated {len(days_data)} calendar days for {data.year}",
        "count": len(days_data)
    }


@router.post("/leave")
async def add_leave_block(
    data: LeaveBlockRequest,
    user: dict = Depends(get_current_user)
):
    """Add a leave block"""
    db = Database()
    
    if data.end_date < data.start_date:
        raise HTTPException(
            status_code=400,
            detail="End date must be after start date"
        )
    
    leave_data = {
        "user_id": user["id"],
        "name": data.name,
        "start_date": data.start_date.isoformat(),
        "end_date": data.end_date.isoformat(),
        "effects": {
            "work": "suspended",
            "available_time": "increased"
        },
        "notes": data.notes
    }
    
    leave_block = await db.create_leave_block(leave_data)
    
    # Update affected calendar days
    calendar_days = await db.get_calendar_days(
        user["id"],
        data.start_date.isoformat(),
        data.end_date.isoformat()
    )
    
    for day in calendar_days:
        state = day.get("state_json", {})
        state["is_leave"] = True
        state["available_hours"] = 16.0
        if "leave" not in state.get("tags", []):
            state.setdefault("tags", []).append("leave")
        
        await db.upsert_calendar_days([{
            "user_id": user["id"],
            "date": day["date"],
            "cycle_id": day.get("cycle_id"),
            "cycle_day": day.get("cycle_day"),
            "work_type": day.get("work_type"),
            "state_json": state
        }])
    
    return {
        "success": True,
        "message": "Leave block added",
        "data": leave_block,
        "affected_days": len(calendar_days)
    }


@router.get("/leave")
async def list_leave_blocks(user: dict = Depends(get_current_user)):
    """Get all leave blocks"""
    db = Database()
    leave_blocks = await db.get_leave_blocks(user["id"])
    
    return {
        "success": True,
        "data": leave_blocks
    }


@router.delete("/leave/{leave_id}")
async def delete_leave_block(
    leave_id: str,
    user: dict = Depends(get_current_user)
):
    """Delete a leave block"""
    db = Database()
    await db.delete_leave_block(leave_id)
    
    return {
        "success": True,
        "message": "Leave block deleted"
    }
