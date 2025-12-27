"""
Daily Logs Routes
CRUD operations for daily work logs and notes
"""

from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from loguru import logger
import csv
import io

from app.database import Database
from app.middleware.auth import get_current_user

router = APIRouter()


class DailyLogCreateRequest(BaseModel):
    """Request to create a daily log"""
    date: str
    note: str
    actual_hours: Optional[float] = None
    overtime_hours: Optional[float] = None


class DailyLogUpdateRequest(BaseModel):
    """Request to update a daily log"""
    note: Optional[str] = None
    actual_hours: Optional[float] = None
    overtime_hours: Optional[float] = None


class HoursUpdateRequest(BaseModel):
    """Request to update hours for a date"""
    actual_hours: float
    overtime_hours: Optional[float] = 0


@router.get("/daily-logs")
async def get_daily_logs(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    user: dict = Depends(get_current_user)
):
    """Get all daily logs for the current user, optionally filtered by date range"""
    logger.info(f"[DAILY_LOGS] Getting logs for user {user['id']}")

    db = Database(use_admin=True)
    logs = await db.get_daily_logs(user["id"], start_date, end_date)

    return logs


@router.get("/daily-logs/{date_str}")
async def get_daily_log_by_date(
    date_str: str,
    user: dict = Depends(get_current_user)
):
    """Get daily log for a specific date"""
    logger.info(f"[DAILY_LOGS] Getting log for date {date_str}")

    db = Database(use_admin=True)
    log = await db.get_daily_log_by_date(user["id"], date_str)

    if not log:
        # Return empty structure for dates without logs
        return {
            "date": date_str,
            "logs": [],
            "actual_hours": None,
            "overtime_hours": None
        }

    return log


@router.post("/daily-logs")
async def create_daily_log(
    request: DailyLogCreateRequest,
    user: dict = Depends(get_current_user)
):
    """Create a new daily log"""
    logger.info(f"[DAILY_LOGS] Creating log for date {request.date}")

    db = Database(use_admin=True)

    log_data = {
        "user_id": user["id"],
        "date": request.date,
        "note": request.note,
        "actual_hours": request.actual_hours,
        "overtime_hours": request.overtime_hours
    }

    result = await db.create_daily_log(log_data)

    if not result:
        raise HTTPException(status_code=500, detail="Failed to create daily log")

    return result


@router.patch("/daily-logs/{log_id}")
async def update_daily_log(
    log_id: str,
    request: DailyLogUpdateRequest,
    user: dict = Depends(get_current_user)
):
    """Update a daily log"""
    logger.info(f"[DAILY_LOGS] Updating log {log_id}")

    db = Database(use_admin=True)

    # Verify ownership
    existing = await db.get_daily_log(log_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Daily log not found")
    if existing["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    update_data = {}
    if request.note is not None:
        update_data["note"] = request.note
    if request.actual_hours is not None:
        update_data["actual_hours"] = request.actual_hours
    if request.overtime_hours is not None:
        update_data["overtime_hours"] = request.overtime_hours

    if not update_data:
        return existing

    result = await db.update_daily_log(log_id, update_data)

    if not result:
        raise HTTPException(status_code=500, detail="Failed to update daily log")

    return result


@router.put("/daily-logs/{date_str}/hours")
async def update_daily_hours(
    date_str: str,
    request: HoursUpdateRequest,
    user: dict = Depends(get_current_user)
):
    """Update or create hours for a specific date"""
    logger.info(f"[DAILY_LOGS] Updating hours for date {date_str}")

    db = Database(use_admin=True)

    hours_data = {
        "actual_hours": request.actual_hours,
        "overtime_hours": request.overtime_hours or 0
    }

    result = await db.update_daily_hours(user["id"], date_str, hours_data)

    if not result:
        raise HTTPException(status_code=500, detail="Failed to update hours")

    return result


@router.delete("/daily-logs/{log_id}")
async def delete_daily_log(
    log_id: str,
    user: dict = Depends(get_current_user)
):
    """Delete a daily log"""
    logger.info(f"[DAILY_LOGS] Deleting log {log_id}")

    db = Database(use_admin=True)

    # Verify ownership
    existing = await db.get_daily_log(log_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Daily log not found")
    if existing["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    success = await db.delete_daily_log(log_id)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete daily log")

    return {"success": True}


@router.get("/daily-logs/export")
async def export_daily_logs(
    start_date: str = Query(...),
    end_date: str = Query(...),
    format: str = Query("csv"),
    user: dict = Depends(get_current_user)
):
    """Export daily logs as CSV or PDF"""
    logger.info(f"[DAILY_LOGS] Exporting logs from {start_date} to {end_date} as {format}")

    db = Database(use_admin=True)
    logs = await db.get_daily_logs(user["id"], start_date, end_date)

    if format == "csv":
        # Generate CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow(["Date", "Note", "Actual Hours", "Overtime Hours", "Created At"])

        # Data
        for log in logs:
            writer.writerow([
                log.get("date", ""),
                log.get("note", ""),
                log.get("actual_hours", ""),
                log.get("overtime_hours", ""),
                log.get("created_at", "")
            ])

        output.seek(0)

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=daily-logs-{start_date}-to-{end_date}.csv"}
        )

    elif format == "pdf":
        # For PDF, we'll return a simple text format that can be converted client-side
        # or you can integrate a PDF library like reportlab
        content = f"Daily Logs Export\n"
        content += f"Period: {start_date} to {end_date}\n"
        content += "=" * 50 + "\n\n"

        for log in logs:
            content += f"Date: {log.get('date', 'N/A')}\n"
            content += f"Note: {log.get('note', 'N/A')}\n"
            content += f"Hours: {log.get('actual_hours', 'N/A')} (Overtime: {log.get('overtime_hours', 0)})\n"
            content += "-" * 30 + "\n"

        return StreamingResponse(
            iter([content]),
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename=daily-logs-{start_date}-to-{end_date}.txt"}
        )

    else:
        raise HTTPException(status_code=400, detail="Invalid format. Use 'csv' or 'pdf'")
