"""
Watchman Stats Routes
Endpoints for statistics and analytics
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from datetime import date
from loguru import logger
import csv
import io
import time
import json

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
    format: str = Query("csv"),
    user: dict = Depends(require_pro_tier)
):
    """Export statistics data as CSV or PDF (Pro tier required)"""
    start_time = time.time()
    logger.info(f"[STATS] === EXPORT STATS ===")
    logger.info(f"[STATS] User: {user['id']}")
    logger.info(f"[STATS] Year: {year}")
    logger.info(f"[STATS] Format requested: {format}")

    db = Database(use_admin=True)
    stats_engine = create_stats_engine(user["id"])

    calendar_days = await db.get_calendar_days(
        user["id"],
        f"{year}-01-01",
        f"{year}-12-31"
    )
    logger.info(f"[STATS] Found {len(calendar_days)} calendar days")

    commitments = await db.get_commitments(user["id"])
    logger.info(f"[STATS] Found {len(commitments)} commitments")

    yearly_stats = stats_engine.compute_yearly_stats(calendar_days, year) or {}
    commitment_stats = stats_engine.compute_commitment_stats(commitments, calendar_days) or []
    distribution = stats_engine.compute_load_distribution(calendar_days) or {}

    logger.info(f"[STATS] Computed yearly_stats: {bool(yearly_stats)}")
    logger.info(f"[STATS] Computed commitment_stats: {len(commitment_stats)} items")
    logger.info(f"[STATS] Computed distribution: {bool(distribution)}")

    if format == "csv":
        logger.info(f"[STATS] Generating CSV export...")
        output = io.StringIO()
        writer = csv.writer(output)

        # Header row
        writer.writerow([
            "Date", "Work Type", "Day of Week", "Study Hours", "Commitments"
        ])

        # Data rows
        for day in calendar_days:
            state = day.get("state_json", {}) or {}
            commitments_list = state.get("commitments", []) or []
            total_hours = sum(c.get("hours", 0) for c in commitments_list)
            commitment_names = ", ".join(c.get("name", "Unknown") for c in commitments_list)

            writer.writerow([
                day.get("date", ""),
                day.get("work_type", ""),
                day.get("day_of_week", ""),
                total_hours,
                commitment_names or "None"
            ])

        output.seek(0)

        elapsed = (time.time() - start_time) * 1000
        logger.info(f"[STATS] CSV export complete: {len(calendar_days)} rows ({elapsed:.2f}ms)")
        logger.info(f"[STATS] Returning StreamingResponse with Content-Disposition: attachment")

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=watchman-stats-{year}.csv"}
        )

    elif format == "pdf":
        logger.info(f"[STATS] Generating PDF/text export...")
        content = "=" * 60 + "\n"
        content += f"WATCHMAN STATISTICS REPORT - {year}\n"
        content += "=" * 60 + "\n\n"
        content += f"Generated: {date.today().isoformat()}\n\n"

        # Summary section
        content += "YEARLY SUMMARY\n"
        content += "-" * 40 + "\n"
        if yearly_stats:
            content += f"Total Days Planned: {yearly_stats.get('total_days', 0)}\n"
            content += f"Work Days: {yearly_stats.get('work_days', 0)}\n"
            content += f"Work Nights: {yearly_stats.get('work_nights', 0)}\n"
            content += f"Off Days: {yearly_stats.get('off_days', 0)}\n"
            content += f"Leave Days: {yearly_stats.get('leave_days', 0)}\n"
            content += f"Total Study Hours: {yearly_stats.get('total_study_hours', 0)}\n"
        else:
            content += "No statistics available\n"
        content += "\n"

        # Commitment breakdown
        content += "COMMITMENT BREAKDOWN\n"
        content += "-" * 40 + "\n"
        if commitment_stats:
            for cs in commitment_stats:
                content += f"  - {cs.get('name', 'Unknown')}: {cs.get('total_hours', 0)} hours\n"
        else:
            content += "No commitment data\n"
        content += "\n"

        # Load distribution
        content += "LOAD DISTRIBUTION BY DAY TYPE\n"
        content += "-" * 40 + "\n"
        if distribution:
            for day_type, data in distribution.items():
                if isinstance(data, dict):
                    content += f"  {day_type}: {data.get('total_hours', 0)} hours across {data.get('count', 0)} days\n"
        else:
            content += "No distribution data\n"

        content += "\n" + "=" * 60 + "\n"
        content += "END OF REPORT\n"
        content += "=" * 60 + "\n"

        elapsed = (time.time() - start_time) * 1000
        logger.info(f"[STATS] Text export complete ({elapsed:.2f}ms)")
        logger.info(f"[STATS] Returning StreamingResponse with Content-Disposition: attachment")

        return StreamingResponse(
            iter([content]),
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename=watchman-stats-{year}.txt"}
        )

    else:
        logger.warning(f"[STATS] Invalid export format requested: {format}")
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid format. Use 'csv' or 'pdf'")


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
