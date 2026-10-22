"""
Watchman Calendar Routes
Endpoints for calendar day management and generation
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from calendar import isleap
from typing import Optional
from datetime import date

from app.database import Database
from app.middleware.auth import get_current_user, get_effective_tier
from app.engines.calendar_authority import projection_for_user
from app.engines.calendar_engine import (
    CALENDAR_ENGINE_VERSION,
    present_calendar_day,
    year_bounds,
)
from app.engines.command_executor import create_command_executor


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
    logger.info(f"[CALENDAR] GET /calendar - user_id: {user['id']}, range: {start_date} to {end_date}")
    db = Database(use_admin=True)

    days = await db.get_calendar_days(
        user["id"],
        start_date.isoformat(),
        end_date.isoformat()
    )
    logger.info(f"[CALENDAR] Returning {len(days)} days for user {user['id']}")

    return {
        "success": True,
        "data": [present_calendar_day(day) for day in (days or [])],
        "count": len(days or [])
    }


def _is_calendar_stale(days: list) -> bool:
    """Check if calendar data was generated with an older engine version."""
    if not days:
        return True
    # Check the first day's engine version
    first_day = days[0]
    state_json = first_day.get("state_json", {})
    stored_version = state_json.get("engine_version", 0)
    return stored_version < CALENDAR_ENGINE_VERSION


def _year_is_incomplete(days: list, year: int) -> bool:
    """True when the year does not contain every date, including pre-anchor days."""
    expected = 366 if isleap(year) else 365
    return len(days or []) < expected


@router.get("/year/{year}")
async def get_year(
    year: int,
    user: dict = Depends(get_current_user)
):
    """Get all calendar days for a specific year. Auto-generates if empty or stale."""
    logger.info(f"[CALENDAR] GET /calendar/year/{year} - user_id: {user['id']}")
    db = Database(use_admin=True)

    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"

    days = await db.get_calendar_days(user["id"], start_date, end_date)
    logger.debug(f"[CALENDAR] Found {len(days)} existing days for year {year}")

    # Check if data is missing OR stale (generated with older engine version)
    needs_regeneration = _is_calendar_stale(days) or _year_is_incomplete(days, year)
    # A failed delete or create leaves ids here. Finish that rebuild before
    # serving the year, and leave the ids if it fails again.
    try:
        executor = create_command_executor(db, user["id"])
        if await executor.finish_pending_removals():
            days = await db.get_calendar_days(user["id"], start_date, end_date)
            needs_regeneration = _is_calendar_stale(days) or _year_is_incomplete(days, year)
    except Exception:
        logger.exception(
            f"Pending commitment removal was not finished for user {user['id']}; "
            "the ids stay until the next calendar read"
        )
    if needs_regeneration:
        logger.info(f"[CALENDAR] Calendar needs regeneration (stale or empty) for user {user['id']}, year {year}")

    if needs_regeneration:
        if days:
            logger.info(f"Calendar data for user {user['id']} year {year} is stale (old engine version), regenerating...")
        else:
            logger.info(f"No calendar data for user {user['id']} year {year}, auto-generating...")
        # Same path as an approved chat change: document leave and
        # leave_blocks are merged, commitments are stamped, overrides stay.
        # Nothing is deleted first. If this load fails, return the days already
        # stored and try again on the next request.
        try:
            start_gen, end_gen = year_bounds(year)
            executor = create_command_executor(db, user["id"])
            await executor.rebuild_calendar(start_gen, end_gen)
            days = await db.get_calendar_days(user["id"], start_date, end_date)
            logger.info(f"Rebuilt {len(days or [])} days for year {year}")
        except Exception:
            logger.exception(
                f"Calendar rebuild failed for user {user['id']} year {year}; "
                "returning the stored days"
            )
    
    shown = [present_calendar_day(day) for day in (days or [])]
    return {
        "success": True,
        "data": shown,
        "year": year,
        "count": len(shown)
    }


@router.get("/month/{year}/{month}")
async def get_month(
    year: int,
    month: int,
    user: dict = Depends(get_current_user)
):
    """Get all calendar days for a specific month"""
    db = Database(use_admin=True)
    
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
    
    shown = [present_calendar_day(day) for day in (days or [])]
    return {
        "success": True,
        "data": shown,
        "year": year,
        "month": month,
        "count": len(shown)
    }


@router.get("/day/{date_str}")
async def get_day(
    date_str: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific calendar day with full details"""
    db = Database(use_admin=True)
    
    day = await db.get_calendar_day(user["id"], date_str)
    
    if not day:
        return {
            "success": True,
            "data": None,
            "message": "No calendar data for this date"
        }
    
    return {
        "success": True,
        "data": present_calendar_day(day)
    }


@router.post("/generate")
async def generate_calendar(
    data: GenerateCalendarRequest,
    user: dict = Depends(get_current_user)
):
    """Generate calendar days for a year based on active cycle"""
    db = Database(use_admin=True)
    
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

    # Master Settings wins. The cycles table is used only when the document has no cycle.
    projection = await projection_for_user(db, user["id"])
    cycle = projection.get("cycle")

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
    
    if (
        existing
        and not data.regenerate
        and not _is_calendar_stale(existing)
        and not _year_is_incomplete(existing, data.year)
    ):
        return {
            "success": True,
            "message": f"Calendar for {data.year} already exists. Set regenerate=true to overwrite.",
            "count": len(existing)
        }
    
    start_date, end_date = year_bounds(data.year)
    executor = create_command_executor(db, user["id"])
    await executor.rebuild_calendar(start_date, end_date)
    days = await db.get_calendar_days(
        user["id"], f"{data.year}-01-01", f"{data.year}-12-31"
    )
    return {
        "success": True,
        "message": f"Generated {len(days or [])} calendar days for {data.year}",
        "count": len(days or []),
    }


@router.post("/leave")
async def add_leave_block(
    data: LeaveBlockRequest,
    user: dict = Depends(get_current_user)
):
    """
    Add a leave block.
    PRO FEATURE: Leave planning is only available for Pro or trial users.
    """
    # Check tier - leave planning is Pro only (trial users get access)
    effective_tier = get_effective_tier(user)
    if effective_tier not in ["pro", "admin", "trial"]:
        raise HTTPException(
            status_code=403,
            detail="Leave planning is a Pro feature. Upgrade to Pro to block out vacation days, sick leave, and plan time off on your calendar!"
        )

    db = Database(use_admin=True)

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
    db = Database(use_admin=True)
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
    db = Database(use_admin=True)
    leaves = await db.get_leave_blocks(user["id"])
    if not any(item.get("id") == leave_id for item in leaves):
        raise HTTPException(status_code=404, detail="Leave block not found")
    deleted = await db.delete_leave_block(leave_id, user_id=user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Leave block not found")
    
    return {
        "success": True,
        "message": "Leave block deleted"
    }
