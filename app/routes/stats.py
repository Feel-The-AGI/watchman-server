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
    """Export comprehensive statistics as CSV or PDF (Pro tier required)"""
    from collections import defaultdict
    from fastapi import HTTPException

    start_time = time.time()
    logger.info(f"[STATS] === EXPORT STATS ===")
    logger.info(f"[STATS] User: {user['id']}, Year: {year}, Format: {format}")

    db = Database(use_admin=True)

    # Fetch all data
    calendar_days = await db.get_calendar_days(user["id"], f"{year}-01-01", f"{year}-12-31")
    commitments = await db.get_commitments(user["id"])
    daily_logs = await db.get_daily_logs(user["id"], f"{year}-01-01", f"{year}-12-31")
    incidents = await db.get_incidents(user["id"], f"{year}-01-01", f"{year}-12-31")

    logger.info(f"[STATS] Data: {len(calendar_days)} days, {len(commitments)} commitments, {len(daily_logs)} logs, {len(incidents)} incidents")

    # Calculate comprehensive stats
    MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]

    # Monthly breakdown
    monthly_stats = defaultdict(lambda: {
        "work_days": 0, "work_nights": 0, "off_days": 0, "leave_days": 0,
        "commitment_hours": 0, "overtime_hours": 0
    })

    # Work type totals
    work_days = work_nights = off_days = leave_days = 0
    total_commitment_hours = 0

    # Commitment tracking
    commitment_hours = defaultdict(float)
    commitment_days = defaultdict(int)

    for day in calendar_days:
        month = int(day.get("date", "2025-01-01").split("-")[1])
        work_type = day.get("work_type", "off")
        state = day.get("state_json", {}) or {}
        is_leave = state.get("is_leave", False)

        # Count work types
        if is_leave:
            leave_days += 1
            monthly_stats[month]["leave_days"] += 1
        elif work_type == "work_day":
            work_days += 1
            monthly_stats[month]["work_days"] += 1
        elif work_type == "work_night":
            work_nights += 1
            monthly_stats[month]["work_nights"] += 1
        else:
            off_days += 1
            monthly_stats[month]["off_days"] += 1

        # Count commitment hours
        for c in state.get("commitments", []) or []:
            hours = c.get("hours", 0)
            name = c.get("name", "Unknown")
            total_commitment_hours += hours
            monthly_stats[month]["commitment_hours"] += hours
            commitment_hours[name] += hours
            commitment_days[name] += 1

    # Process daily logs for overtime
    total_overtime = 0
    logs_by_date = {log.get("date"): log for log in daily_logs}
    for log in daily_logs:
        overtime = log.get("overtime_hours", 0) or 0
        total_overtime += overtime
        month = int(log.get("date", "2025-01-01").split("-")[1])
        monthly_stats[month]["overtime_hours"] += overtime

    # Incident counts
    incident_counts = defaultdict(int)
    for inc in incidents:
        incident_counts[inc.get("type", "other")] += 1

    if format == "csv":
        logger.info(f"[STATS] Generating comprehensive CSV...")
        output = io.StringIO()
        writer = csv.writer(output)

        # ===== SECTION 1: HEADER =====
        writer.writerow(["WATCHMAN ANNUAL REPORT"])
        writer.writerow([f"Year: {year}"])
        writer.writerow([f"Generated: {date.today().strftime('%B %d, %Y')}"])
        writer.writerow([])

        # ===== SECTION 2: YEARLY SUMMARY =====
        writer.writerow(["=== YEARLY SUMMARY ==="])
        writer.writerow(["Metric", "Value"])
        writer.writerow(["Total Days Planned", len(calendar_days)])
        writer.writerow(["Day Shifts", work_days])
        writer.writerow(["Night Shifts", work_nights])
        writer.writerow(["Off Days", off_days])
        writer.writerow(["Leave Days", leave_days])
        writer.writerow(["Total Commitment Hours", round(total_commitment_hours, 1)])
        writer.writerow(["Total Overtime Hours", round(total_overtime, 1)])
        writer.writerow(["Incidents Logged", len(incidents)])
        writer.writerow([])

        # ===== SECTION 3: MONTHLY BREAKDOWN =====
        writer.writerow(["=== MONTHLY BREAKDOWN ==="])
        writer.writerow(["Month", "Day Shifts", "Night Shifts", "Off Days", "Leave", "Commitment Hrs", "Overtime Hrs"])
        for m in range(1, 13):
            ms = monthly_stats[m]
            writer.writerow([
                MONTH_NAMES[m],
                ms["work_days"],
                ms["work_nights"],
                ms["off_days"],
                ms["leave_days"],
                round(ms["commitment_hours"], 1),
                round(ms["overtime_hours"], 1)
            ])
        writer.writerow([])

        # ===== SECTION 4: COMMITMENT BREAKDOWN =====
        writer.writerow(["=== COMMITMENT BREAKDOWN ==="])
        writer.writerow(["Commitment", "Total Hours", "Days Active", "Avg Hours/Day"])
        for name in sorted(commitment_hours.keys()):
            hours = commitment_hours[name]
            days = commitment_days[name]
            avg = round(hours / days, 1) if days > 0 else 0
            writer.writerow([name, round(hours, 1), days, avg])
        writer.writerow([])

        # ===== SECTION 5: INCIDENT SUMMARY =====
        if incidents:
            writer.writerow(["=== INCIDENT SUMMARY ==="])
            writer.writerow(["Type", "Count"])
            for inc_type, count in sorted(incident_counts.items()):
                writer.writerow([inc_type.replace("_", " ").title(), count])
            writer.writerow([])

        # ===== SECTION 6: DAILY DETAILS =====
        writer.writerow(["=== DAILY SCHEDULE DETAILS ==="])
        writer.writerow(["Date", "Day", "Work Type", "Leave?", "Commitment Hours", "Overtime", "Commitments"])
        for day in calendar_days:
            d = day.get("date", "")
            state = day.get("state_json", {}) or {}
            commits = state.get("commitments", []) or []
            total_hrs = sum(c.get("hours", 0) for c in commits)
            commit_names = ", ".join(f"{c.get('name', '?')} ({c.get('hours', 0)}h)" for c in commits) or "-"
            log = logs_by_date.get(d, {})
            overtime = log.get("overtime_hours", 0) or 0

            writer.writerow([
                d,
                day.get("day_of_week", "")[:3],
                day.get("work_type", "off").replace("_", " ").title(),
                "Yes" if state.get("is_leave") else "",
                round(total_hrs, 1) if total_hrs else "",
                round(overtime, 1) if overtime else "",
                commit_names
            ])

        output.seek(0)
        elapsed = (time.time() - start_time) * 1000
        logger.info(f"[STATS] CSV complete: {len(calendar_days)} days ({elapsed:.2f}ms)")

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=watchman-report-{year}.csv"}
        )

    elif format == "pdf":
        logger.info(f"[STATS] Generating PDF report...")
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import inch
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
            from reportlab.lib.enums import TA_CENTER, TA_LEFT

            buffer = io.BytesIO()
            doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=0.5*inch, bottomMargin=0.5*inch)
            styles = getSampleStyleSheet()
            elements = []

            # Custom styles
            title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=24, alignment=TA_CENTER, spaceAfter=20)
            subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'], fontSize=12, alignment=TA_CENTER, textColor=colors.grey)
            section_style = ParagraphStyle('Section', parent=styles['Heading2'], fontSize=14, spaceBefore=20, spaceAfter=10)

            # Title
            elements.append(Paragraph("Watchman Annual Report", title_style))
            elements.append(Paragraph(f"{year}", title_style))
            elements.append(Paragraph(f"Generated: {date.today().strftime('%B %d, %Y')}", subtitle_style))
            elements.append(Spacer(1, 30))

            # Summary Section
            elements.append(Paragraph("Yearly Summary", section_style))
            summary_data = [
                ["Metric", "Value"],
                ["Total Days Planned", str(len(calendar_days))],
                ["Day Shifts", str(work_days)],
                ["Night Shifts", str(work_nights)],
                ["Off Days", str(off_days)],
                ["Leave Days", str(leave_days)],
                ["Total Commitment Hours", f"{round(total_commitment_hours, 1)}"],
                ["Total Overtime Hours", f"{round(total_overtime, 1)}"],
                ["Incidents Logged", str(len(incidents))],
            ]
            summary_table = Table(summary_data, colWidths=[3*inch, 2*inch])
            summary_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8B5CF6')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F5F5F5')]),
            ]))
            elements.append(summary_table)
            elements.append(Spacer(1, 20))

            # Monthly Breakdown
            elements.append(Paragraph("Monthly Breakdown", section_style))
            monthly_data = [["Month", "Days", "Nights", "Off", "Leave", "Commit Hrs", "OT Hrs"]]
            for m in range(1, 13):
                ms = monthly_stats[m]
                monthly_data.append([
                    MONTH_NAMES[m][:3],
                    str(ms["work_days"]),
                    str(ms["work_nights"]),
                    str(ms["off_days"]),
                    str(ms["leave_days"]),
                    str(round(ms["commitment_hours"], 1)),
                    str(round(ms["overtime_hours"], 1))
                ])
            monthly_table = Table(monthly_data, colWidths=[0.8*inch, 0.7*inch, 0.7*inch, 0.6*inch, 0.6*inch, 1*inch, 0.7*inch])
            monthly_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8B5CF6')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F5F5F5')]),
            ]))
            elements.append(monthly_table)
            elements.append(Spacer(1, 20))

            # Commitment Breakdown
            if commitment_hours:
                elements.append(Paragraph("Commitment Breakdown", section_style))
                commit_data = [["Commitment", "Total Hours", "Days Active", "Avg/Day"]]
                for name in sorted(commitment_hours.keys()):
                    hours = commitment_hours[name]
                    days = commitment_days[name]
                    avg = round(hours / days, 1) if days > 0 else 0
                    commit_data.append([name, str(round(hours, 1)), str(days), str(avg)])
                commit_table = Table(commit_data, colWidths=[2.5*inch, 1.2*inch, 1*inch, 1*inch])
                commit_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#10B981')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                    ('TOPPADDING', (0, 0), (-1, -1), 8),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F5F5F5')]),
                ]))
                elements.append(commit_table)

            # Incidents
            if incidents:
                elements.append(Spacer(1, 20))
                elements.append(Paragraph("Incident Summary", section_style))
                inc_data = [["Type", "Count"]]
                for inc_type, count in sorted(incident_counts.items()):
                    inc_data.append([inc_type.replace("_", " ").title(), str(count)])
                inc_table = Table(inc_data, colWidths=[3*inch, 1.5*inch])
                inc_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#EF4444')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('ALIGN', (1, 0), (1, -1), 'CENTER'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                    ('TOPPADDING', (0, 0), (-1, -1), 8),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ]))
                elements.append(inc_table)

            # Build PDF
            doc.build(elements)
            buffer.seek(0)

            elapsed = (time.time() - start_time) * 1000
            logger.info(f"[STATS] PDF complete ({elapsed:.2f}ms)")

            return StreamingResponse(
                buffer,
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename=watchman-report-{year}.pdf"}
            )

        except ImportError as e:
            logger.error(f"[STATS] reportlab not installed: {e}")
            raise HTTPException(status_code=500, detail="PDF generation unavailable. Please use CSV export.")

    else:
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
