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
            from reportlab.lib.pagesizes import A4, letter
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import inch, mm
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
            from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
            from reportlab.graphics.shapes import Drawing, Rect, String, Line
            from reportlab.graphics import renderPDF

            # Brand colors
            PURPLE = colors.HexColor('#8B5CF6')
            PURPLE_LIGHT = colors.HexColor('#A78BFA')
            PURPLE_DARK = colors.HexColor('#7C3AED')
            GREEN = colors.HexColor('#10B981')
            GREEN_LIGHT = colors.HexColor('#34D399')
            RED = colors.HexColor('#EF4444')
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

            # ========== HEADER BANNER ==========
            header = Drawing(page_width, 100)
            # Gradient-like background
            header.add(Rect(0, 0, page_width, 100, fillColor=PURPLE_DARK, strokeColor=None))
            header.add(Rect(0, 0, page_width * 0.7, 100, fillColor=PURPLE, strokeColor=None))
            # Decorative accent
            header.add(Rect(page_width - 80, 0, 80, 100, fillColor=PURPLE_LIGHT, strokeColor=None))
            # Title text
            header.add(String(30, 65, "WATCHMAN", fontName="Helvetica-Bold", fontSize=28, fillColor=colors.white))
            header.add(String(30, 40, f"Annual Report {year}", fontName="Helvetica", fontSize=16, fillColor=colors.white))
            header.add(String(30, 18, f"Generated {date.today().strftime('%B %d, %Y')}", fontName="Helvetica", fontSize=10, fillColor=colors.HexColor('#E0E0E0')))
            elements.append(header)
            elements.append(Spacer(1, 25))

            # ========== KEY METRICS CARDS ==========
            # Create a row of metric cards
            total_shifts = work_days + work_nights
            metrics = [
                ("Total Days", str(len(calendar_days)), PURPLE),
                ("Day Shifts", str(work_days), GREEN),
                ("Night Shifts", str(work_nights), PURPLE_LIGHT),
                ("Off Days", str(off_days), GRAY),
            ]

            card_width = (page_width - 30) / 4
            metrics_drawing = Drawing(page_width, 70)
            for i, (label, value, color) in enumerate(metrics):
                x = i * (card_width + 10)
                # Card background with rounded effect (using rect)
                metrics_drawing.add(Rect(x, 0, card_width, 65, fillColor=GRAY_LIGHT, strokeColor=colors.HexColor('#E5E7EB'), strokeWidth=1, rx=5, ry=5))
                # Color accent bar at top
                metrics_drawing.add(Rect(x, 55, card_width, 10, fillColor=color, strokeColor=None, rx=5, ry=5))
                metrics_drawing.add(Rect(x, 55, card_width, 5, fillColor=color, strokeColor=None))
                # Value
                metrics_drawing.add(String(x + card_width/2 - 10, 28, value, fontName="Helvetica-Bold", fontSize=20, fillColor=DARK_BG))
                # Label
                metrics_drawing.add(String(x + card_width/2 - len(label)*2.5, 10, label, fontName="Helvetica", fontSize=9, fillColor=GRAY))
            elements.append(metrics_drawing)
            elements.append(Spacer(1, 20))

            # Second row of metrics
            metrics2 = [
                ("Leave Days", str(leave_days), AMBER),
                ("Commitment Hrs", str(round(total_commitment_hours, 1)), GREEN),
                ("Overtime Hrs", str(round(total_overtime, 1)), RED),
                ("Incidents", str(len(incidents)), RED if incidents else GRAY),
            ]
            metrics_drawing2 = Drawing(page_width, 70)
            for i, (label, value, color) in enumerate(metrics2):
                x = i * (card_width + 10)
                metrics_drawing2.add(Rect(x, 0, card_width, 65, fillColor=GRAY_LIGHT, strokeColor=colors.HexColor('#E5E7EB'), strokeWidth=1, rx=5, ry=5))
                metrics_drawing2.add(Rect(x, 55, card_width, 10, fillColor=color, strokeColor=None, rx=5, ry=5))
                metrics_drawing2.add(Rect(x, 55, card_width, 5, fillColor=color, strokeColor=None))
                metrics_drawing2.add(String(x + card_width/2 - len(value)*5, 28, value, fontName="Helvetica-Bold", fontSize=20, fillColor=DARK_BG))
                metrics_drawing2.add(String(x + card_width/2 - len(label)*2.5, 10, label, fontName="Helvetica", fontSize=9, fillColor=GRAY))
            elements.append(metrics_drawing2)
            elements.append(Spacer(1, 30))

            # ========== MONTHLY BREAKDOWN TABLE ==========
            # Section header with accent bar
            section_header = Drawing(page_width, 30)
            section_header.add(Rect(0, 0, 5, 25, fillColor=PURPLE, strokeColor=None))
            section_header.add(String(15, 8, "Monthly Breakdown", fontName="Helvetica-Bold", fontSize=14, fillColor=DARK_BG))
            elements.append(section_header)
            elements.append(Spacer(1, 10))

            monthly_data = [["Month", "Day", "Night", "Off", "Leave", "Commit", "OT"]]
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

            col_widths = [0.9*inch, 0.65*inch, 0.65*inch, 0.6*inch, 0.6*inch, 0.8*inch, 0.6*inch]
            monthly_table = Table(monthly_data, colWidths=col_widths)
            monthly_table.setStyle(TableStyle([
                # Header row
                ('BACKGROUND', (0, 0), (-1, 0), PURPLE),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                # Body
                ('FONTSIZE', (0, 1), (-1, -1), 9),
                ('FONTNAME', (0, 1), (0, -1), 'Helvetica-Bold'),  # Month names bold
                ('TEXTCOLOR', (0, 1), (0, -1), PURPLE),  # Month names purple
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                # Alternating rows
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, GRAY_LIGHT]),
                # Borders
                ('LINEBELOW', (0, 0), (-1, 0), 2, PURPLE),
                ('LINEBELOW', (0, -1), (-1, -1), 1, colors.HexColor('#E5E7EB')),
                ('LINEBEFORE', (0, 0), (0, -1), 0, colors.white),
            ]))
            elements.append(monthly_table)
            elements.append(Spacer(1, 30))

            # ========== COMMITMENT BREAKDOWN ==========
            if commitment_hours:
                section_header2 = Drawing(page_width, 30)
                section_header2.add(Rect(0, 0, 5, 25, fillColor=GREEN, strokeColor=None))
                section_header2.add(String(15, 8, "Commitment Breakdown", fontName="Helvetica-Bold", fontSize=14, fillColor=DARK_BG))
                elements.append(section_header2)
                elements.append(Spacer(1, 10))

                commit_data = [["Commitment", "Total Hours", "Days Active", "Avg/Day"]]
                for name in sorted(commitment_hours.keys()):
                    hours = commitment_hours[name]
                    days = commitment_days[name]
                    avg = round(hours / days, 1) if days > 0 else 0
                    commit_data.append([name, str(round(hours, 1)), str(days), str(avg)])

                commit_table = Table(commit_data, colWidths=[2.5*inch, 1.2*inch, 1*inch, 1*inch])
                commit_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), GREEN),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
                    ('TOPPADDING', (0, 0), (-1, -1), 10),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, GRAY_LIGHT]),
                    ('LINEBELOW', (0, 0), (-1, 0), 2, GREEN),
                ]))
                elements.append(commit_table)
                elements.append(Spacer(1, 30))

            # ========== INCIDENTS ==========
            if incidents:
                section_header3 = Drawing(page_width, 30)
                section_header3.add(Rect(0, 0, 5, 25, fillColor=RED, strokeColor=None))
                section_header3.add(String(15, 8, "Incident Summary", fontName="Helvetica-Bold", fontSize=14, fillColor=DARK_BG))
                elements.append(section_header3)
                elements.append(Spacer(1, 10))

                inc_data = [["Type", "Count", "Severity Distribution"]]
                for inc_type, count in sorted(incident_counts.items()):
                    # Simple severity indicator
                    severity_text = "●" * min(count, 5) + ("+" if count > 5 else "")
                    inc_data.append([inc_type.replace("_", " ").title(), str(count), severity_text])

                inc_table = Table(inc_data, colWidths=[2.5*inch, 1*inch, 2*inch])
                inc_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), RED),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('TEXTCOLOR', (2, 1), (2, -1), RED),  # Severity dots in red
                    ('ALIGN', (1, 0), (1, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
                    ('TOPPADDING', (0, 0), (-1, -1), 10),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FEF2F2')]),
                    ('LINEBELOW', (0, 0), (-1, 0), 2, RED),
                ]))
                elements.append(inc_table)

            # ========== FOOTER ==========
            elements.append(Spacer(1, 40))
            footer = Drawing(page_width, 40)
            footer.add(Line(0, 35, page_width, 35, strokeColor=colors.HexColor('#E5E7EB'), strokeWidth=1))
            footer.add(String(0, 15, "Watchman - Shift Worker Calendar & Analytics", fontName="Helvetica", fontSize=9, fillColor=GRAY))
            footer.add(String(0, 3, "This report was automatically generated. Data is accurate as of the generation date.", fontName="Helvetica", fontSize=7, fillColor=colors.HexColor('#9CA3AF')))
            footer.add(String(page_width - 80, 10, f"Page 1", fontName="Helvetica", fontSize=8, fillColor=GRAY))
            elements.append(footer)

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
