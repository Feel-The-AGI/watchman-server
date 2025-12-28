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
import time

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
    start_time = time.time()
    logger.info(f"[DAILY_LOGS] === GET DAILY LOGS ===")
    logger.info(f"[DAILY_LOGS] User: {user['id']}")
    logger.info(f"[DAILY_LOGS] Date range: {start_date or 'all'} to {end_date or 'all'}")

    db = Database(use_admin=True)
    logs = await db.get_daily_logs(user["id"], start_date, end_date)

    elapsed = (time.time() - start_time) * 1000
    logger.info(f"[DAILY_LOGS] Found {len(logs)} logs in {elapsed:.2f}ms")
    return logs


# IMPORTANT: Export route must come BEFORE /{date_str} to avoid being caught by the parameter route
@router.get("/daily-logs/export")
async def export_daily_logs(
    start_date: str = Query(...),
    end_date: str = Query(...),
    format: str = Query("csv"),
    user: dict = Depends(get_current_user)
):
    """Export daily logs as CSV or PDF"""
    start_time = time.time()
    logger.info(f"[DAILY_LOGS] === EXPORT DAILY LOGS ===")
    logger.info(f"[DAILY_LOGS] User: {user['id']}")
    logger.info(f"[DAILY_LOGS] Date range: {start_date} to {end_date}")
    logger.info(f"[DAILY_LOGS] Format: {format}")

    db = Database(use_admin=True)
    logs = await db.get_daily_logs(user["id"], start_date, end_date)
    logger.info(f"[DAILY_LOGS] Found {len(logs)} logs to export")

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

        elapsed = (time.time() - start_time) * 1000
        logger.info(f"[DAILY_LOGS] CSV export complete: {len(logs)} rows ({elapsed:.2f}ms)")

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=daily-logs-{start_date}-to-{end_date}.csv"}
        )

    elif format == "pdf":
        logger.info(f"[DAILY_LOGS] Generating PDF report...")
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import inch
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.graphics.shapes import Drawing, Rect, String, Line

            # Brand colors
            BLUE = colors.HexColor('#3B82F6')
            BLUE_LIGHT = colors.HexColor('#93C5FD')
            BLUE_DARK = colors.HexColor('#1D4ED8')
            PURPLE = colors.HexColor('#8B5CF6')
            GREEN = colors.HexColor('#10B981')
            AMBER = colors.HexColor('#F59E0B')
            GRAY = colors.HexColor('#6B7280')
            GRAY_LIGHT = colors.HexColor('#F3F4F6')
            DARK_BG = colors.HexColor('#1A1A2E')

            buffer = io.BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=A4,
                topMargin=0.4*inch,
                bottomMargin=0.6*inch,
                leftMargin=0.6*inch,
                rightMargin=0.6*inch
            )
            elements = []
            page_width = A4[0] - 1.2*inch

            # Calculate summaries
            total_actual = sum(log.get('actual_hours', 0) or 0 for log in logs)
            total_overtime = sum(log.get('overtime_hours', 0) or 0 for log in logs)
            days_with_notes = sum(1 for log in logs if log.get('note'))
            days_with_overtime = sum(1 for log in logs if (log.get('overtime_hours', 0) or 0) > 0)

            # ========== HEADER BANNER ==========
            header = Drawing(page_width, 100)
            header.add(Rect(0, 0, page_width, 100, fillColor=BLUE_DARK, strokeColor=None))
            header.add(Rect(0, 0, page_width * 0.7, 100, fillColor=BLUE, strokeColor=None))
            header.add(Rect(page_width - 80, 0, 80, 100, fillColor=BLUE_LIGHT, strokeColor=None))
            header.add(String(30, 65, "DAILY LOGS REPORT", fontName="Helvetica-Bold", fontSize=24, fillColor=colors.white))
            header.add(String(30, 40, f"Period: {start_date} to {end_date}", fontName="Helvetica", fontSize=14, fillColor=colors.white))
            header.add(String(30, 18, f"Total Entries: {len(logs)}", fontName="Helvetica", fontSize=10, fillColor=colors.HexColor('#E0E0E0')))
            elements.append(header)
            elements.append(Spacer(1, 25))

            # ========== SUMMARY CARDS ==========
            card_width = (page_width - 30) / 4
            summary_drawing = Drawing(page_width, 70)
            summary_items = [
                ('Total Entries', str(len(logs)), BLUE),
                ('Actual Hours', str(round(total_actual, 1)), GREEN),
                ('Overtime Hrs', str(round(total_overtime, 1)), AMBER),
                ('Days w/ OT', str(days_with_overtime), PURPLE),
            ]
            for i, (label, value, color) in enumerate(summary_items):
                x = i * (card_width + 10)
                summary_drawing.add(Rect(x, 0, card_width, 65, fillColor=GRAY_LIGHT, strokeColor=colors.HexColor('#E5E7EB'), strokeWidth=1, rx=5, ry=5))
                summary_drawing.add(Rect(x, 55, card_width, 10, fillColor=color, strokeColor=None, rx=5, ry=5))
                summary_drawing.add(Rect(x, 55, card_width, 5, fillColor=color, strokeColor=None))
                summary_drawing.add(String(x + card_width/2 - len(value)*5, 28, value, fontName="Helvetica-Bold", fontSize=20, fillColor=DARK_BG))
                summary_drawing.add(String(x + card_width/2 - len(label)*2.5, 10, label, fontName="Helvetica", fontSize=9, fillColor=GRAY))
            elements.append(summary_drawing)
            elements.append(Spacer(1, 25))

            # ========== DAILY ENTRIES TABLE ==========
            if logs:
                section_header = Drawing(page_width, 30)
                section_header.add(Rect(0, 0, 5, 25, fillColor=BLUE, strokeColor=None))
                section_header.add(String(15, 8, "Daily Entries", fontName="Helvetica-Bold", fontSize=14, fillColor=DARK_BG))
                elements.append(section_header)
                elements.append(Spacer(1, 10))

                # Create styles for paragraphs
                styles = getSampleStyleSheet()
                note_style = ParagraphStyle('note', parent=styles['Normal'], fontSize=8, leading=10)

                log_data = [["Date", "Hours", "OT", "Notes"]]
                for log in logs:
                    note = log.get('note', '') or ''
                    # Truncate long notes
                    if len(note) > 80:
                        note = note[:80] + '...'
                    log_data.append([
                        log.get('date', 'N/A'),
                        str(log.get('actual_hours', '-') or '-'),
                        str(log.get('overtime_hours', '-') or '-'),
                        Paragraph(note if note else '-', note_style)
                    ])

                log_table = Table(log_data, colWidths=[1.1*inch, 0.8*inch, 0.6*inch, 3.8*inch])
                log_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), BLUE),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 10),
                    ('FONTSIZE', (0, 1), (-1, -1), 9),
                    ('ALIGN', (0, 0), (2, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                    ('TOPPADDING', (0, 0), (-1, -1), 8),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#EFF6FF')]),
                    ('LINEBELOW', (0, 0), (-1, 0), 2, BLUE),
                ]))
                elements.append(log_table)

            # ========== FOOTER ==========
            elements.append(Spacer(1, 40))
            footer = Drawing(page_width, 40)
            footer.add(Line(0, 35, page_width, 35, strokeColor=colors.HexColor('#E5E7EB'), strokeWidth=1))
            footer.add(String(0, 15, "Watchman - Daily Work Logs", fontName="Helvetica", fontSize=9, fillColor=GRAY))
            footer.add(String(0, 3, "This report was automatically generated.", fontName="Helvetica", fontSize=7, fillColor=colors.HexColor('#9CA3AF')))
            elements.append(footer)

            # Build PDF
            doc.build(elements)
            buffer.seek(0)

            elapsed = (time.time() - start_time) * 1000
            logger.info(f"[DAILY_LOGS] PDF complete ({elapsed:.2f}ms)")

            return StreamingResponse(
                buffer,
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename=daily-logs-{start_date}-to-{end_date}.pdf"}
            )

        except ImportError as e:
            logger.error(f"[DAILY_LOGS] reportlab not installed: {e}")
            raise HTTPException(status_code=500, detail="PDF generation unavailable. Please use CSV export.")

    else:
        logger.warning(f"[DAILY_LOGS] Invalid export format requested: {format}")
        raise HTTPException(status_code=400, detail="Invalid format. Use 'csv' or 'pdf'")


@router.get("/daily-logs/{date_str}")
async def get_daily_log_by_date(
    date_str: str,
    user: dict = Depends(get_current_user)
):
    """Get daily log for a specific date"""
    start_time = time.time()
    logger.info(f"[DAILY_LOGS] === GET LOG BY DATE ===")
    logger.info(f"[DAILY_LOGS] User: {user['id']}")
    logger.info(f"[DAILY_LOGS] Date: {date_str}")

    db = Database(use_admin=True)
    log = await db.get_daily_log_by_date(user["id"], date_str)

    elapsed = (time.time() - start_time) * 1000
    if not log:
        logger.info(f"[DAILY_LOGS] No log found for {date_str}, returning empty structure ({elapsed:.2f}ms)")
        return {
            "date": date_str,
            "logs": [],
            "actual_hours": None,
            "overtime_hours": None
        }

    logger.info(f"[DAILY_LOGS] Log found: id={log.get('id')} ({elapsed:.2f}ms)")
    return log


@router.post("/daily-logs")
async def create_daily_log(
    request: DailyLogCreateRequest,
    user: dict = Depends(get_current_user)
):
    """Create a new daily log"""
    start_time = time.time()
    logger.info(f"[DAILY_LOGS] === CREATE DAILY LOG ===")
    logger.info(f"[DAILY_LOGS] User: {user['id']}")
    logger.info(f"[DAILY_LOGS] Date: {request.date}")
    logger.info(f"[DAILY_LOGS] Note length: {len(request.note)} chars")
    logger.info(f"[DAILY_LOGS] Hours: actual={request.actual_hours}, overtime={request.overtime_hours}")

    db = Database(use_admin=True)

    log_data = {
        "user_id": user["id"],
        "date": request.date,
        "note": request.note,
        "actual_hours": request.actual_hours,
        "overtime_hours": request.overtime_hours
    }

    result = await db.create_daily_log(log_data)

    elapsed = (time.time() - start_time) * 1000
    if not result:
        logger.error(f"[DAILY_LOGS] Failed to create log ({elapsed:.2f}ms)")
        raise HTTPException(status_code=500, detail="Failed to create daily log")

    logger.info(f"[DAILY_LOGS] Log created: id={result.get('id')} ({elapsed:.2f}ms)")
    return result


@router.patch("/daily-logs/{log_id}")
async def update_daily_log(
    log_id: str,
    request: DailyLogUpdateRequest,
    user: dict = Depends(get_current_user)
):
    """Update a daily log"""
    start_time = time.time()
    logger.info(f"[DAILY_LOGS] === UPDATE DAILY LOG ===")
    logger.info(f"[DAILY_LOGS] User: {user['id']}")
    logger.info(f"[DAILY_LOGS] Log ID: {log_id}")

    db = Database(use_admin=True)

    # Verify ownership
    existing = await db.get_daily_log(log_id)
    if not existing:
        logger.warning(f"[DAILY_LOGS] Log not found: {log_id}")
        raise HTTPException(status_code=404, detail="Daily log not found")
    if existing["user_id"] != user["id"]:
        logger.warning(f"[DAILY_LOGS] Unauthorized access attempt: user {user['id']} tried to update log owned by {existing['user_id']}")
        raise HTTPException(status_code=403, detail="Not authorized")

    update_data = {}
    if request.note is not None:
        update_data["note"] = request.note
        logger.debug(f"[DAILY_LOGS] Updating note: {len(request.note)} chars")
    if request.actual_hours is not None:
        update_data["actual_hours"] = request.actual_hours
        logger.debug(f"[DAILY_LOGS] Updating actual_hours: {request.actual_hours}")
    if request.overtime_hours is not None:
        update_data["overtime_hours"] = request.overtime_hours
        logger.debug(f"[DAILY_LOGS] Updating overtime_hours: {request.overtime_hours}")

    if not update_data:
        logger.info(f"[DAILY_LOGS] No fields to update, returning existing")
        return existing

    result = await db.update_daily_log(log_id, update_data)

    elapsed = (time.time() - start_time) * 1000
    if not result:
        logger.error(f"[DAILY_LOGS] Failed to update log ({elapsed:.2f}ms)")
        raise HTTPException(status_code=500, detail="Failed to update daily log")

    logger.info(f"[DAILY_LOGS] Log updated successfully ({elapsed:.2f}ms)")
    return result


@router.put("/daily-logs/{date_str}/hours")
async def update_daily_hours(
    date_str: str,
    request: HoursUpdateRequest,
    user: dict = Depends(get_current_user)
):
    """Update or create hours for a specific date"""
    start_time = time.time()
    logger.info(f"[DAILY_LOGS] === UPDATE DAILY HOURS ===")
    logger.info(f"[DAILY_LOGS] User: {user['id']}")
    logger.info(f"[DAILY_LOGS] Date: {date_str}")
    logger.info(f"[DAILY_LOGS] Hours: actual={request.actual_hours}, overtime={request.overtime_hours}")

    db = Database(use_admin=True)

    hours_data = {
        "actual_hours": request.actual_hours,
        "overtime_hours": request.overtime_hours or 0
    }

    result = await db.update_daily_hours(user["id"], date_str, hours_data)

    elapsed = (time.time() - start_time) * 1000
    if not result:
        logger.error(f"[DAILY_LOGS] Failed to update hours ({elapsed:.2f}ms)")
        raise HTTPException(status_code=500, detail="Failed to update hours")

    logger.info(f"[DAILY_LOGS] Hours updated successfully ({elapsed:.2f}ms)")
    return result


@router.delete("/daily-logs/{log_id}")
async def delete_daily_log(
    log_id: str,
    user: dict = Depends(get_current_user)
):
    """Delete a daily log"""
    start_time = time.time()
    logger.info(f"[DAILY_LOGS] === DELETE DAILY LOG ===")
    logger.info(f"[DAILY_LOGS] User: {user['id']}")
    logger.info(f"[DAILY_LOGS] Log ID: {log_id}")

    db = Database(use_admin=True)

    # Verify ownership
    existing = await db.get_daily_log(log_id)
    if not existing:
        logger.warning(f"[DAILY_LOGS] Log not found for deletion: {log_id}")
        raise HTTPException(status_code=404, detail="Daily log not found")
    if existing["user_id"] != user["id"]:
        logger.warning(f"[DAILY_LOGS] Unauthorized delete attempt: user {user['id']} tried to delete log owned by {existing['user_id']}")
        raise HTTPException(status_code=403, detail="Not authorized")

    success = await db.delete_daily_log(log_id)

    elapsed = (time.time() - start_time) * 1000
    if not success:
        logger.error(f"[DAILY_LOGS] Failed to delete log ({elapsed:.2f}ms)")
        raise HTTPException(status_code=500, detail="Failed to delete daily log")

    logger.info(f"[DAILY_LOGS] Log deleted successfully ({elapsed:.2f}ms)")
    return {"success": True}
