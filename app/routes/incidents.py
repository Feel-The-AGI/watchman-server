"""
Incidents Routes
CRUD operations for workplace incidents and issues tracking
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
from app.services.email_service import get_email_service

router = APIRouter()


class IncidentCreateRequest(BaseModel):
    """Request to create an incident"""
    date: str
    type: str  # overtime, safety, equipment, harassment, injury, policy_violation, other
    severity: str = "medium"  # low, medium, high, critical
    title: str
    description: str
    reported_to: Optional[str] = None
    witnesses: Optional[str] = None
    outcome: Optional[str] = None


class IncidentUpdateRequest(BaseModel):
    """Request to update an incident"""
    type: Optional[str] = None
    severity: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    reported_to: Optional[str] = None
    witnesses: Optional[str] = None
    outcome: Optional[str] = None


VALID_TYPES = [
    "overtime", "safety", "equipment", "harassment", "injury", "policy_violation",
    "health", "discrimination", "workload", "compensation", "scheduling",
    "communication", "retaliation", "environment", "other"
]
VALID_SEVERITIES = ["low", "medium", "high", "critical"]


@router.get("/incidents")
async def get_incidents(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    user: dict = Depends(get_current_user)
):
    """Get all incidents for the current user, optionally filtered by date range"""
    start_time = time.time()
    logger.info(f"[INCIDENTS] === GET INCIDENTS ===")
    logger.info(f"[INCIDENTS] User: {user['id']}")
    logger.info(f"[INCIDENTS] Date range: {start_date or 'all'} to {end_date or 'all'}")

    db = Database(use_admin=True)
    incidents = await db.get_incidents(user["id"], start_date, end_date)

    elapsed = (time.time() - start_time) * 1000
    logger.info(f"[INCIDENTS] Found {len(incidents)} incidents in {elapsed:.2f}ms")
    return incidents


@router.get("/incidents/stats")
async def get_incident_stats(
    year: Optional[int] = Query(None),
    user: dict = Depends(get_current_user)
):
    """Get incident statistics for the current user"""
    start_time = time.time()
    logger.info(f"[INCIDENTS] === GET INCIDENT STATS ===")
    logger.info(f"[INCIDENTS] User: {user['id']}")
    logger.info(f"[INCIDENTS] Year: {year or 'all time'}")

    db = Database(use_admin=True)
    stats = await db.get_incident_stats(user["id"], year)

    elapsed = (time.time() - start_time) * 1000
    logger.info(f"[INCIDENTS] Stats retrieved: total={stats.get('total_count', 0)} ({elapsed:.2f}ms)")
    logger.debug(f"[INCIDENTS] By type: {stats.get('by_type', {})}")
    logger.debug(f"[INCIDENTS] By severity: {stats.get('by_severity', {})}")
    return stats


@router.get("/incidents/date/{date_str}")
async def get_incidents_by_date(
    date_str: str,
    user: dict = Depends(get_current_user)
):
    """Get all incidents for a specific date"""
    start_time = time.time()
    logger.info(f"[INCIDENTS] === GET INCIDENTS BY DATE ===")
    logger.info(f"[INCIDENTS] User: {user['id']}")
    logger.info(f"[INCIDENTS] Date: {date_str}")

    db = Database(use_admin=True)
    incidents = await db.get_incidents_by_date(user["id"], date_str)

    elapsed = (time.time() - start_time) * 1000
    logger.info(f"[INCIDENTS] Found {len(incidents)} incidents for {date_str} ({elapsed:.2f}ms)")
    return incidents


# IMPORTANT: Export route must come BEFORE /{incident_id} to avoid being caught by the parameter route
@router.get("/incidents/export")
async def export_incidents(
    start_date: str = Query(...),
    end_date: str = Query(...),
    format: str = Query("csv"),
    user: dict = Depends(get_current_user)
):
    """Export incidents as CSV or PDF"""
    start_time = time.time()
    logger.info(f"[INCIDENTS] === EXPORT INCIDENTS ===")
    logger.info(f"[INCIDENTS] User: {user['id']}")
    logger.info(f"[INCIDENTS] Date range: {start_date} to {end_date}")
    logger.info(f"[INCIDENTS] Format: {format}")

    db = Database(use_admin=True)
    incidents = await db.get_incidents(user["id"], start_date, end_date)
    logger.info(f"[INCIDENTS] Found {len(incidents)} incidents to export")

    if format == "csv":
        # Generate CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow([
            "Date", "Type", "Severity", "Title", "Description",
            "Reported To", "Witnesses", "Outcome", "Created At"
        ])

        # Data
        for incident in incidents:
            writer.writerow([
                incident.get("date", ""),
                incident.get("type", ""),
                incident.get("severity", ""),
                incident.get("title", ""),
                incident.get("description", ""),
                incident.get("reported_to", ""),
                incident.get("witnesses", ""),
                incident.get("outcome", ""),
                incident.get("created_at", "")
            ])

        output.seek(0)

        elapsed = (time.time() - start_time) * 1000
        logger.info(f"[INCIDENTS] CSV export complete: {len(incidents)} rows ({elapsed:.2f}ms)")

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=incidents-{start_date}-to-{end_date}.csv"}
        )

    elif format == "pdf":
        logger.info(f"[INCIDENTS] Generating PDF report...")
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import inch
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.graphics.shapes import Drawing, Rect, String, Line

            # Brand colors
            RED = colors.HexColor('#EF4444')
            RED_LIGHT = colors.HexColor('#FCA5A5')
            RED_DARK = colors.HexColor('#DC2626')
            AMBER = colors.HexColor('#F59E0B')
            GREEN = colors.HexColor('#10B981')
            PURPLE = colors.HexColor('#8B5CF6')
            GRAY = colors.HexColor('#6B7280')
            GRAY_LIGHT = colors.HexColor('#F3F4F6')
            DARK_BG = colors.HexColor('#1A1A2E')

            # Severity colors
            SEVERITY_COLORS = {
                'critical': RED_DARK,
                'high': RED,
                'medium': AMBER,
                'low': GREEN
            }

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
            type_counts = {}
            severity_counts = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
            for incident in incidents:
                t = incident.get("type", "other")
                s = incident.get("severity", "medium")
                type_counts[t] = type_counts.get(t, 0) + 1
                severity_counts[s] = severity_counts.get(s, 0) + 1

            # ========== HEADER BANNER ==========
            header = Drawing(page_width, 100)
            header.add(Rect(0, 0, page_width, 100, fillColor=RED_DARK, strokeColor=None))
            header.add(Rect(0, 0, page_width * 0.7, 100, fillColor=RED, strokeColor=None))
            header.add(Rect(page_width - 80, 0, 80, 100, fillColor=RED_LIGHT, strokeColor=None))
            header.add(String(30, 65, "INCIDENT REPORT", fontName="Helvetica-Bold", fontSize=24, fillColor=colors.white))
            header.add(String(30, 40, f"Period: {start_date} to {end_date}", fontName="Helvetica", fontSize=14, fillColor=colors.white))
            header.add(String(30, 18, f"Total Incidents: {len(incidents)}", fontName="Helvetica", fontSize=10, fillColor=colors.HexColor('#E0E0E0')))
            elements.append(header)
            elements.append(Spacer(1, 25))

            # ========== SEVERITY SUMMARY CARDS ==========
            card_width = (page_width - 30) / 4
            severity_drawing = Drawing(page_width, 70)
            severity_order = [('critical', 'Critical'), ('high', 'High'), ('medium', 'Medium'), ('low', 'Low')]
            for i, (sev_key, sev_label) in enumerate(severity_order):
                x = i * (card_width + 10)
                color = SEVERITY_COLORS.get(sev_key, GRAY)
                count = severity_counts.get(sev_key, 0)
                severity_drawing.add(Rect(x, 0, card_width, 65, fillColor=GRAY_LIGHT, strokeColor=colors.HexColor('#E5E7EB'), strokeWidth=1, rx=5, ry=5))
                severity_drawing.add(Rect(x, 55, card_width, 10, fillColor=color, strokeColor=None, rx=5, ry=5))
                severity_drawing.add(Rect(x, 55, card_width, 5, fillColor=color, strokeColor=None))
                severity_drawing.add(String(x + card_width/2 - 5, 28, str(count), fontName="Helvetica-Bold", fontSize=20, fillColor=DARK_BG))
                severity_drawing.add(String(x + card_width/2 - len(sev_label)*3, 10, sev_label, fontName="Helvetica", fontSize=9, fillColor=GRAY))
            elements.append(severity_drawing)
            elements.append(Spacer(1, 25))

            # ========== TYPE BREAKDOWN ==========
            if type_counts:
                section_header = Drawing(page_width, 30)
                section_header.add(Rect(0, 0, 5, 25, fillColor=PURPLE, strokeColor=None))
                section_header.add(String(15, 8, "Incidents by Type", fontName="Helvetica-Bold", fontSize=14, fillColor=DARK_BG))
                elements.append(section_header)
                elements.append(Spacer(1, 10))

                type_data = [["Type", "Count", "Percentage"]]
                for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                    pct = f"{(count/len(incidents)*100):.1f}%" if incidents else "0%"
                    type_data.append([t.replace('_', ' ').title(), str(count), pct])

                type_table = Table(type_data, colWidths=[3*inch, 1.2*inch, 1.2*inch])
                type_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), PURPLE),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
                    ('TOPPADDING', (0, 0), (-1, -1), 10),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, GRAY_LIGHT]),
                    ('LINEBELOW', (0, 0), (-1, 0), 2, PURPLE),
                ]))
                elements.append(type_table)
                elements.append(Spacer(1, 25))

            # ========== INCIDENT DETAILS ==========
            if incidents:
                section_header2 = Drawing(page_width, 30)
                section_header2.add(Rect(0, 0, 5, 25, fillColor=RED, strokeColor=None))
                section_header2.add(String(15, 8, "Incident Details", fontName="Helvetica-Bold", fontSize=14, fillColor=DARK_BG))
                elements.append(section_header2)
                elements.append(Spacer(1, 10))

                # Create styles for paragraphs
                styles = getSampleStyleSheet()
                desc_style = ParagraphStyle('desc', parent=styles['Normal'], fontSize=9, leading=12)

                details_data = [["Date", "Type", "Severity", "Title", "Description"]]
                for incident in incidents:
                    sev = incident.get('severity', 'medium')
                    # Truncate description for table
                    desc = incident.get('description', '')[:100]
                    if len(incident.get('description', '')) > 100:
                        desc += '...'
                    details_data.append([
                        incident.get('date', 'N/A'),
                        incident.get('type', 'N/A').replace('_', ' ').title(),
                        sev.title(),
                        incident.get('title', 'N/A')[:30],
                        Paragraph(desc, desc_style)
                    ])

                details_table = Table(details_data, colWidths=[0.9*inch, 1*inch, 0.8*inch, 1.3*inch, 2.3*inch])
                details_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), RED),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 9),
                    ('FONTSIZE', (0, 1), (-1, -1), 8),
                    ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                    ('TOPPADDING', (0, 0), (-1, -1), 8),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FEF2F2')]),
                    ('LINEBELOW', (0, 0), (-1, 0), 2, RED),
                ]))
                elements.append(details_table)

            # ========== FOOTER ==========
            elements.append(Spacer(1, 40))
            footer = Drawing(page_width, 40)
            footer.add(Line(0, 35, page_width, 35, strokeColor=colors.HexColor('#E5E7EB'), strokeWidth=1))
            footer.add(String(0, 15, "Watchman - Incident Tracking & Documentation", fontName="Helvetica", fontSize=9, fillColor=GRAY))
            footer.add(String(0, 3, "This report is confidential. Handle according to workplace policy.", fontName="Helvetica", fontSize=7, fillColor=colors.HexColor('#9CA3AF')))
            elements.append(footer)

            # Build PDF
            doc.build(elements)
            buffer.seek(0)

            elapsed = (time.time() - start_time) * 1000
            logger.info(f"[INCIDENTS] PDF complete ({elapsed:.2f}ms)")

            return StreamingResponse(
                buffer,
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename=incident-report-{start_date}-to-{end_date}.pdf"}
            )

        except ImportError as e:
            logger.error(f"[INCIDENTS] reportlab not installed: {e}")
            raise HTTPException(status_code=500, detail="PDF generation unavailable. Please use CSV export.")

    else:
        logger.warning(f"[INCIDENTS] Invalid export format requested: {format}")
        raise HTTPException(status_code=400, detail="Invalid format. Use 'csv' or 'pdf'")


@router.get("/incidents/{incident_id}")
async def get_incident(
    incident_id: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific incident by ID"""
    start_time = time.time()
    logger.info(f"[INCIDENTS] === GET INCIDENT ===")
    logger.info(f"[INCIDENTS] User: {user['id']}")
    logger.info(f"[INCIDENTS] Incident ID: {incident_id}")

    db = Database(use_admin=True)
    incident = await db.get_incident(incident_id)

    elapsed = (time.time() - start_time) * 1000
    if not incident:
        logger.warning(f"[INCIDENTS] Incident not found: {incident_id} ({elapsed:.2f}ms)")
        raise HTTPException(status_code=404, detail="Incident not found")

    if incident["user_id"] != user["id"]:
        logger.warning(f"[INCIDENTS] Unauthorized access: user {user['id']} tried to view incident owned by {incident['user_id']}")
        raise HTTPException(status_code=403, detail="Not authorized")

    logger.info(f"[INCIDENTS] Incident found: type={incident.get('type')}, severity={incident.get('severity')} ({elapsed:.2f}ms)")
    return incident


@router.post("/incidents")
async def create_incident(
    request: IncidentCreateRequest,
    user: dict = Depends(get_current_user)
):
    """Create a new incident"""
    start_time = time.time()
    logger.info(f"[INCIDENTS] === CREATE INCIDENT ===")
    logger.info(f"[INCIDENTS] User: {user['id']}")
    logger.info(f"[INCIDENTS] Date: {request.date}")
    logger.info(f"[INCIDENTS] Type: {request.type}")
    logger.info(f"[INCIDENTS] Severity: {request.severity}")
    logger.info(f"[INCIDENTS] Title: {request.title[:50]}...")

    # Validate type
    if request.type not in VALID_TYPES:
        logger.warning(f"[INCIDENTS] Invalid type: {request.type}")
        raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {VALID_TYPES}")

    # Validate severity
    if request.severity not in VALID_SEVERITIES:
        logger.warning(f"[INCIDENTS] Invalid severity: {request.severity}")
        raise HTTPException(status_code=400, detail=f"Invalid severity. Must be one of: {VALID_SEVERITIES}")

    db = Database(use_admin=True)

    incident_data = {
        "user_id": user["id"],
        "date": request.date,
        "type": request.type,
        "severity": request.severity,
        "title": request.title,
        "description": request.description,
        "reported_to": request.reported_to,
        "witnesses": request.witnesses,
        "outcome": request.outcome
    }

    result = await db.create_incident(incident_data)

    elapsed = (time.time() - start_time) * 1000
    if not result:
        logger.error(f"[INCIDENTS] Failed to create incident ({elapsed:.2f}ms)")
        raise HTTPException(status_code=500, detail="Failed to create incident")

    logger.info(f"[INCIDENTS] Incident created: id={result.get('id')}, type={request.type}, severity={request.severity} ({elapsed:.2f}ms)")

    # Send email notification if enabled
    user_settings = user.get("settings", {})
    if user_settings.get("notifications_email", False):
        try:
            email_service = get_email_service()
            user_email = user.get("email")
            user_name = user.get("name") or user_email.split("@")[0] if user_email else "there"

            if user_email:
                await email_service.send_incident_alert(
                    to=user_email,
                    user_name=user_name,
                    incident_title=request.title,
                    incident_type=request.type,
                    severity=request.severity,
                    description=request.description or "",
                )
                logger.info(f"[INCIDENTS] Email notification sent to {user_email}")
        except Exception as e:
            # Don't fail the request if email fails
            logger.warning(f"[INCIDENTS] Failed to send email notification: {e}")

    return result


@router.patch("/incidents/{incident_id}")
async def update_incident(
    incident_id: str,
    request: IncidentUpdateRequest,
    user: dict = Depends(get_current_user)
):
    """Update an incident"""
    start_time = time.time()
    logger.info(f"[INCIDENTS] === UPDATE INCIDENT ===")
    logger.info(f"[INCIDENTS] User: {user['id']}")
    logger.info(f"[INCIDENTS] Incident ID: {incident_id}")

    db = Database(use_admin=True)

    # Verify ownership
    existing = await db.get_incident(incident_id)
    if not existing:
        logger.warning(f"[INCIDENTS] Incident not found: {incident_id}")
        raise HTTPException(status_code=404, detail="Incident not found")
    if existing["user_id"] != user["id"]:
        logger.warning(f"[INCIDENTS] Unauthorized update: user {user['id']} tried to update incident owned by {existing['user_id']}")
        raise HTTPException(status_code=403, detail="Not authorized")

    # Validate type if provided
    if request.type:
        if request.type not in VALID_TYPES:
            logger.warning(f"[INCIDENTS] Invalid type in update: {request.type}")
            raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {VALID_TYPES}")

    # Validate severity if provided
    if request.severity:
        if request.severity not in VALID_SEVERITIES:
            logger.warning(f"[INCIDENTS] Invalid severity in update: {request.severity}")
            raise HTTPException(status_code=400, detail=f"Invalid severity. Must be one of: {VALID_SEVERITIES}")

    update_data = {}
    update_fields = []
    if request.type is not None:
        update_data["type"] = request.type
        update_fields.append(f"type={request.type}")
    if request.severity is not None:
        update_data["severity"] = request.severity
        update_fields.append(f"severity={request.severity}")
    if request.title is not None:
        update_data["title"] = request.title
        update_fields.append("title")
    if request.description is not None:
        update_data["description"] = request.description
        update_fields.append("description")
    if request.reported_to is not None:
        update_data["reported_to"] = request.reported_to
        update_fields.append("reported_to")
    if request.witnesses is not None:
        update_data["witnesses"] = request.witnesses
        update_fields.append("witnesses")
    if request.outcome is not None:
        update_data["outcome"] = request.outcome
        update_fields.append("outcome")

    if not update_data:
        logger.info(f"[INCIDENTS] No fields to update, returning existing")
        return existing

    logger.debug(f"[INCIDENTS] Updating fields: {', '.join(update_fields)}")

    result = await db.update_incident(incident_id, update_data)

    elapsed = (time.time() - start_time) * 1000
    if not result:
        logger.error(f"[INCIDENTS] Failed to update incident ({elapsed:.2f}ms)")
        raise HTTPException(status_code=500, detail="Failed to update incident")

    logger.info(f"[INCIDENTS] Incident updated: {len(update_fields)} fields ({elapsed:.2f}ms)")
    return result


@router.delete("/incidents/{incident_id}")
async def delete_incident(
    incident_id: str,
    user: dict = Depends(get_current_user)
):
    """Delete an incident"""
    start_time = time.time()
    logger.info(f"[INCIDENTS] === DELETE INCIDENT ===")
    logger.info(f"[INCIDENTS] User: {user['id']}")
    logger.info(f"[INCIDENTS] Incident ID: {incident_id}")

    db = Database(use_admin=True)

    # Verify ownership
    existing = await db.get_incident(incident_id)
    if not existing:
        logger.warning(f"[INCIDENTS] Incident not found for deletion: {incident_id}")
        raise HTTPException(status_code=404, detail="Incident not found")
    if existing["user_id"] != user["id"]:
        logger.warning(f"[INCIDENTS] Unauthorized delete: user {user['id']} tried to delete incident owned by {existing['user_id']}")
        raise HTTPException(status_code=403, detail="Not authorized")

    logger.info(f"[INCIDENTS] Deleting incident: type={existing.get('type')}, severity={existing.get('severity')}, date={existing.get('date')}")

    success = await db.delete_incident(incident_id)

    elapsed = (time.time() - start_time) * 1000
    if not success:
        logger.error(f"[INCIDENTS] Failed to delete incident ({elapsed:.2f}ms)")
        raise HTTPException(status_code=500, detail="Failed to delete incident")

    logger.info(f"[INCIDENTS] Incident deleted successfully ({elapsed:.2f}ms)")
    return {"success": True}
