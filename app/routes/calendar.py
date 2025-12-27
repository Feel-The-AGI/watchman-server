"""
Watchman Calendar Routes
Endpoints for calendar day management and generation
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from datetime import date

from app.database import Database
from app.middleware.auth import get_current_user, get_effective_tier
from app.engines.calendar_engine import create_calendar_engine, CALENDAR_ENGINE_VERSION


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
        "data": days,
        "count": len(days)
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
    needs_regeneration = _is_calendar_stale(days)
    if needs_regeneration:
        logger.info(f"[CALENDAR] Calendar needs regeneration (stale or empty) for user {user['id']}, year {year}")

    if needs_regeneration:
        if days:
            logger.info(f"Calendar data for user {user['id']} year {year} is stale (old engine version), regenerating...")
        else:
            logger.info(f"No calendar data for user {user['id']} year {year}, auto-generating...")
        cycle = await db.get_active_cycle(user["id"])
        if cycle:
            logger.info(f"Auto-generating calendar for year {year}, user {user['id']}")
            
            # Normalize cycle for engine
            raw_pattern = cycle.get("pattern", [])
            engine_pattern = []
            for block in raw_pattern:
                if "label" in block:
                    engine_pattern.append({"label": block["label"], "duration": block["duration"]})
                elif "type" in block:
                    engine_pattern.append({"label": block["type"], "duration": block.get("days", block.get("duration", 5))})
                else:
                    engine_pattern.append(block)
            
            anchor_date_str = cycle.get("anchor_date")
            anchor_cycle_day = cycle.get("anchor_cycle_day", 1)
            
            cycle_for_engine = {
                "id": cycle.get("id"),
                "anchor_date": anchor_date_str,
                "anchor_cycle_day": anchor_cycle_day,
                "cycle_length": cycle.get("cycle_length") or cycle.get("total_days") or sum(b.get("duration", 0) for b in raw_pattern),
                "pattern": engine_pattern
            }
            
            if anchor_date_str:
                try:
                    from datetime import date as date_module
                    anchor_date = date_module.fromisoformat(anchor_date_str) if isinstance(anchor_date_str, str) else anchor_date_str
                    # Start from anchor date if it's in the requested year, otherwise start of year
                    start_gen = max(date_module(year, 1, 1), anchor_date)
                    end_gen = date_module(year, 12, 31)
                    
                    # Fetch leave blocks for proper regeneration
                    leave_blocks = await db.get_leave_blocks(user["id"])

                    engine = create_calendar_engine(user["id"])
                    gen_days = engine.generate_range(
                        start_gen,
                        end_gen,
                        cycle_for_engine,
                        leave_blocks
                    )

                    # Fetch existing days that have manual_override flag to preserve them
                    existing_days_result = await db.get_calendar_days(
                        user["id"],
                        start_gen.isoformat(),
                        end_gen.isoformat()
                    )
                    manual_override_days = {}
                    for existing_day in (existing_days_result or []):
                        state = existing_day.get("state_json", {})
                        if state.get("manual_override"):
                            manual_override_days[existing_day["date"]] = existing_day

                    if manual_override_days:
                        logger.info(f"Preserving {len(manual_override_days)} manually overridden days during auto-regeneration")

                    days_data = []
                    for d in gen_days:
                        date_str = d.date.isoformat()
                        if date_str in manual_override_days:
                            # Preserve manual override
                            override = manual_override_days[date_str]
                            days_data.append({
                                "user_id": user["id"],
                                "date": date_str,
                                "cycle_id": cycle["id"],
                                "cycle_day": d.cycle_day,
                                "work_type": override["work_type"],
                                "state_json": override["state_json"]
                            })
                        else:
                            days_data.append({
                                "user_id": user["id"],
                                "date": date_str,
                                "cycle_id": cycle["id"],
                                "cycle_day": d.cycle_day,
                                "work_type": d.work_type.value,
                                "state_json": d.state_json
                            })

                    await db.upsert_calendar_days(days_data)
                    days = await db.get_calendar_days(user["id"], start_date, end_date)
                    logger.info(f"Auto-generated {len(days)} days for year {year}")
                except Exception as e:
                    logger.error(f"Failed to auto-generate calendar for {year}: {e}")
    
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
        "data": day
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
    
    # Normalize cycle format for calendar engine
    # Handle both cycles table format and master_settings format
    raw_pattern = cycle.get("pattern", [])
    engine_pattern = []
    for block in raw_pattern:
        if "label" in block:
            engine_pattern.append({"label": block["label"], "duration": block["duration"]})
        elif "type" in block:
            engine_pattern.append({"label": block["type"], "duration": block.get("days", block.get("duration", 5))})
        else:
            engine_pattern.append(block)
    
    # Handle anchor format - support both nested and flat
    anchor_date_str = None
    anchor_cycle_day = 1
    if isinstance(cycle.get("anchor"), dict):
        anchor_date_str = cycle["anchor"].get("date")
        anchor_cycle_day = cycle["anchor"].get("cycle_day", 1)
    if cycle.get("anchor_date"):
        anchor_date_str = cycle.get("anchor_date") or anchor_date_str
        anchor_cycle_day = cycle.get("anchor_cycle_day") or anchor_cycle_day
    
    # Build normalized cycle for engine
    cycle_for_engine = {
        "id": cycle.get("id"),
        "anchor_date": anchor_date_str,
        "anchor_cycle_day": anchor_cycle_day,
        "cycle_length": cycle.get("cycle_length") or cycle.get("total_days") or sum(b.get("duration", b.get("days", 0)) for b in raw_pattern),
        "pattern": engine_pattern
    }
    
    if anchor_date_str:
        from datetime import date as date_module
        anchor_date = date_module.fromisoformat(anchor_date_str) if isinstance(anchor_date_str, str) else anchor_date_str
        # Start from anchor date, not Jan 1
        start_date = max(date_module(data.year, 1, 1), anchor_date)
        end_date = date_module(data.year, 12, 31)
        
        engine = create_calendar_engine(user["id"])
        days = engine.generate_range(start_date, end_date, cycle_for_engine, leave_blocks)
    else:
        # No anchor - generate full year
        engine = create_calendar_engine(user["id"])
        days = engine.generate_year(data.year, cycle_for_engine, leave_blocks)

    # Fetch existing days that have manual_override flag to preserve them
    existing_days = await db.get_calendar_days(
        user["id"],
        f"{data.year}-01-01",
        f"{data.year}-12-31"
    )
    manual_override_days = {}
    for existing_day in (existing_days or []):
        state = existing_day.get("state_json", {})
        if state.get("manual_override"):
            manual_override_days[existing_day["date"]] = existing_day

    if manual_override_days:
        logger.info(f"Preserving {len(manual_override_days)} manually overridden days during calendar generation")

    # Convert to dictionaries for database, preserving manual overrides
    days_data = []
    for d in days:
        date_str = d.date.isoformat()
        if date_str in manual_override_days:
            # Preserve manual override
            override = manual_override_days[date_str]
            days_data.append({
                "user_id": user["id"],
                "date": date_str,
                "cycle_id": cycle["id"],
                "cycle_day": d.cycle_day,
                "work_type": override["work_type"],
                "state_json": override["state_json"]
            })
        else:
            days_data.append({
                "user_id": user["id"],
                "date": date_str,
                "cycle_id": cycle["id"],
                "cycle_day": d.cycle_day,
                "work_type": d.work_type.value,
                "state_json": d.state_json
            })

    # Delete existing from the start date forward, then insert new
    if days_data:
        first_date = days_data[0]["date"]
        await db.delete_calendar_days(user["id"], first_date, f"{data.year}-12-31")

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
    await db.delete_leave_block(leave_id)
    
    return {
        "success": True,
        "message": "Leave block deleted"
    }
