"""
Watchman Stats Routes
Endpoints for statistics and analytics
"""

from fastapi import APIRouter, Depends, Query
from datetime import date

from app.database import Database
from app.middleware.auth import get_current_user, require_pro_tier
from app.engines.stats_engine import create_stats_engine


router = APIRouter()


@router.get("/dashboard")
async def get_dashboard_stats(user: dict = Depends(get_current_user)):
    """Get quick statistics for the dashboard"""
    db = Database(use_admin=True)
    stats_engine = create_stats_engine(user["id"])
    
    # Get recent data
    today = date.today()
    year = today.year
    
    calendar_days = await db.get_calendar_days(
        user["id"],
        today.isoformat(),
        f"{year}-12-31"
    )
    
    commitments = await db.get_commitments(user["id"])
    mutations = await db.get_pending_mutations(user["id"])
    leave_blocks = await db.get_leave_blocks(user["id"])
    
    stats = stats_engine.compute_dashboard_stats(
        calendar_days,
        commitments,
        mutations,
        leave_blocks
    )
    
    return {
        "success": True,
        "data": stats
    }


@router.get("/year/{year}")
async def get_yearly_stats(
    year: int,
    user: dict = Depends(get_current_user)
):
    """Get comprehensive statistics for a full year"""
    db = Database(use_admin=True)
    stats_engine = create_stats_engine(user["id"])
    
    calendar_days = await db.get_calendar_days(
        user["id"],
        f"{year}-01-01",
        f"{year}-12-31"
    )
    
    if not calendar_days:
        return {
            "success": True,
            "data": None,
            "message": f"No calendar data for {year}"
        }
    
    stats = stats_engine.compute_yearly_stats(calendar_days, year)
    
    return {
        "success": True,
        "data": stats
    }


@router.get("/month/{year}/{month}")
async def get_monthly_stats(
    year: int,
    month: int,
    user: dict = Depends(get_current_user)
):
    """Get statistics for a specific month"""
    db = Database(use_admin=True)
    stats_engine = create_stats_engine(user["id"])
    
    # Get month boundaries
    start_date = f"{year}-{month:02d}-01"
    
    if month == 12:
        end_date = f"{year}-12-31"
    else:
        from datetime import timedelta
        next_month = date(year, month + 1, 1)
        last_day = next_month - timedelta(days=1)
        end_date = last_day.isoformat()
    
    calendar_days = await db.get_calendar_days(user["id"], start_date, end_date)
    
    if not calendar_days:
        return {
            "success": True,
            "data": None,
            "message": f"No calendar data for {year}-{month:02d}"
        }
    
    stats = stats_engine.compute_monthly_stats(calendar_days, year, month)
    
    return {
        "success": True,
        "data": stats
    }


@router.get("/commitments")
async def get_commitment_stats(user: dict = Depends(get_current_user)):
    """Get statistics for each commitment"""
    db = Database(use_admin=True)
    stats_engine = create_stats_engine(user["id"])
    
    commitments = await db.get_commitments(user["id"])
    
    year = date.today().year
    calendar_days = await db.get_calendar_days(
        user["id"],
        f"{year}-01-01",
        f"{year}-12-31"
    )
    
    stats = stats_engine.compute_commitment_stats(commitments, calendar_days)
    
    return {
        "success": True,
        "data": stats
    }


@router.get("/load-distribution")
async def get_load_distribution(
    year: int = Query(default=None),
    user: dict = Depends(get_current_user)
):
    """Get how study/commitment load is distributed across day types"""
    db = Database(use_admin=True)
    stats_engine = create_stats_engine(user["id"])
    
    if year is None:
        year = date.today().year
    
    calendar_days = await db.get_calendar_days(
        user["id"],
        f"{year}-01-01",
        f"{year}-12-31"
    )
    
    distribution = stats_engine.compute_load_distribution(calendar_days)
    
    return {
        "success": True,
        "data": distribution,
        "year": year
    }


@router.get("/export")
async def export_stats(
    year: int,
    format: str = "json",
    user: dict = Depends(require_pro_tier)
):
    """Export statistics data (Pro tier required)"""
    db = Database(use_admin=True)
    stats_engine = create_stats_engine(user["id"])
    
    calendar_days = await db.get_calendar_days(
        user["id"],
        f"{year}-01-01",
        f"{year}-12-31"
    )
    
    commitments = await db.get_commitments(user["id"])
    
    yearly_stats = stats_engine.compute_yearly_stats(calendar_days, year)
    commitment_stats = stats_engine.compute_commitment_stats(commitments, calendar_days)
    distribution = stats_engine.compute_load_distribution(calendar_days)
    
    export_data = {
        "year": year,
        "generated_at": date.today().isoformat(),
        "yearly_summary": yearly_stats,
        "commitment_breakdown": commitment_stats,
        "load_distribution": distribution,
        "calendar_days": calendar_days
    }
    
    if format == "json":
        return {
            "success": True,
            "data": export_data
        }
    else:
        # For CSV, we'd convert to CSV format
        # For now, return JSON with a note
        return {
            "success": True,
            "data": export_data,
            "note": "CSV export coming soon"
        }


@router.get("/summary")
async def get_quick_summary(user: dict = Depends(get_current_user)):
    """Get a quick text summary of current state"""
    db = Database(use_admin=True)
    
    today = date.today()
    year = today.year
    
    # Get basic counts
    calendar_days = await db.get_calendar_days(
        user["id"],
        f"{year}-01-01",
        f"{year}-12-31"
    )
    
    commitments = await db.get_active_commitments(user["id"])
    pending_mutations = await db.get_pending_mutations(user["id"])
    
    # Calculate quick stats
    work_days = sum(1 for d in calendar_days if d.get("work_type") == "work_day")
    work_nights = sum(1 for d in calendar_days if d.get("work_type") == "work_night")
    off_days = sum(1 for d in calendar_days if d.get("work_type") == "off")
    leave_days = sum(1 for d in calendar_days if d.get("state_json", {}).get("is_leave"))
    
    total_study_hours = sum(
        sum(c.get("hours", 0) for c in d.get("state_json", {}).get("commitments", [])
            if c.get("type") in ["study", "education"])
        for d in calendar_days
    )
    
    summary = f"""
**{year} Overview**
- {len(calendar_days)} days planned
- {work_days} day shifts | {work_nights} night shifts | {off_days} off days
- {len(commitments)} active commitments
- {round(total_study_hours, 1)} total study hours scheduled
- {len(pending_mutations)} proposals pending review
"""
    
    return {
        "success": True,
        "summary": summary.strip(),
        "data": {
            "year": year,
            "total_days": len(calendar_days),
            "work_days": work_days,
            "work_nights": work_nights,
            "off_days": off_days,
            "leave_days": leave_days,
            "study_hours": round(total_study_hours, 1),
            "commitment_count": len(commitments),
            "pending_proposals": len(pending_mutations)
        }
    }
