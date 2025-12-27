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

from app.database import Database
from app.middleware.auth import get_current_user

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


@router.get("/incidents")
async def get_incidents(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    user: dict = Depends(get_current_user)
):
    """Get all incidents for the current user, optionally filtered by date range"""
    logger.info(f"[INCIDENTS] Getting incidents for user {user['id']}")

    db = Database(use_admin=True)
    incidents = await db.get_incidents(user["id"], start_date, end_date)

    return incidents


@router.get("/incidents/stats")
async def get_incident_stats(
    year: Optional[int] = Query(None),
    user: dict = Depends(get_current_user)
):
    """Get incident statistics for the current user"""
    logger.info(f"[INCIDENTS] Getting stats for user {user['id']}, year={year}")

    db = Database(use_admin=True)
    stats = await db.get_incident_stats(user["id"], year)

    return stats


@router.get("/incidents/date/{date_str}")
async def get_incidents_by_date(
    date_str: str,
    user: dict = Depends(get_current_user)
):
    """Get all incidents for a specific date"""
    logger.info(f"[INCIDENTS] Getting incidents for date {date_str}")

    db = Database(use_admin=True)
    incidents = await db.get_incidents_by_date(user["id"], date_str)

    return incidents


@router.get("/incidents/{incident_id}")
async def get_incident(
    incident_id: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific incident by ID"""
    logger.info(f"[INCIDENTS] Getting incident {incident_id}")

    db = Database(use_admin=True)
    incident = await db.get_incident(incident_id)

    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    if incident["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    return incident


@router.post("/incidents")
async def create_incident(
    request: IncidentCreateRequest,
    user: dict = Depends(get_current_user)
):
    """Create a new incident"""
    logger.info(f"[INCIDENTS] Creating incident for date {request.date}, type={request.type}")

    # Validate type
    valid_types = ["overtime", "safety", "equipment", "harassment", "injury", "policy_violation", "other"]
    if request.type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {valid_types}")

    # Validate severity
    valid_severities = ["low", "medium", "high", "critical"]
    if request.severity not in valid_severities:
        raise HTTPException(status_code=400, detail=f"Invalid severity. Must be one of: {valid_severities}")

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

    if not result:
        raise HTTPException(status_code=500, detail="Failed to create incident")

    return result


@router.patch("/incidents/{incident_id}")
async def update_incident(
    incident_id: str,
    request: IncidentUpdateRequest,
    user: dict = Depends(get_current_user)
):
    """Update an incident"""
    logger.info(f"[INCIDENTS] Updating incident {incident_id}")

    db = Database(use_admin=True)

    # Verify ownership
    existing = await db.get_incident(incident_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Incident not found")
    if existing["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Validate type if provided
    if request.type:
        valid_types = ["overtime", "safety", "equipment", "harassment", "injury", "policy_violation", "other"]
        if request.type not in valid_types:
            raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {valid_types}")

    # Validate severity if provided
    if request.severity:
        valid_severities = ["low", "medium", "high", "critical"]
        if request.severity not in valid_severities:
            raise HTTPException(status_code=400, detail=f"Invalid severity. Must be one of: {valid_severities}")

    update_data = {}
    if request.type is not None:
        update_data["type"] = request.type
    if request.severity is not None:
        update_data["severity"] = request.severity
    if request.title is not None:
        update_data["title"] = request.title
    if request.description is not None:
        update_data["description"] = request.description
    if request.reported_to is not None:
        update_data["reported_to"] = request.reported_to
    if request.witnesses is not None:
        update_data["witnesses"] = request.witnesses
    if request.outcome is not None:
        update_data["outcome"] = request.outcome

    if not update_data:
        return existing

    result = await db.update_incident(incident_id, update_data)

    if not result:
        raise HTTPException(status_code=500, detail="Failed to update incident")

    return result


@router.delete("/incidents/{incident_id}")
async def delete_incident(
    incident_id: str,
    user: dict = Depends(get_current_user)
):
    """Delete an incident"""
    logger.info(f"[INCIDENTS] Deleting incident {incident_id}")

    db = Database(use_admin=True)

    # Verify ownership
    existing = await db.get_incident(incident_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Incident not found")
    if existing["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    success = await db.delete_incident(incident_id)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete incident")

    return {"success": True}


@router.get("/incidents/export")
async def export_incidents(
    start_date: str = Query(...),
    end_date: str = Query(...),
    format: str = Query("csv"),
    user: dict = Depends(get_current_user)
):
    """Export incidents as CSV or PDF"""
    logger.info(f"[INCIDENTS] Exporting incidents from {start_date} to {end_date} as {format}")

    db = Database(use_admin=True)
    incidents = await db.get_incidents(user["id"], start_date, end_date)

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

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=incidents-{start_date}-to-{end_date}.csv"}
        )

    elif format == "pdf":
        # Generate detailed text report
        content = "=" * 60 + "\n"
        content += "INCIDENT REPORT\n"
        content += "=" * 60 + "\n\n"
        content += f"Period: {start_date} to {end_date}\n"
        content += f"Total Incidents: {len(incidents)}\n\n"

        # Summary by type
        type_counts = {}
        severity_counts = {}
        for incident in incidents:
            t = incident.get("type", "other")
            s = incident.get("severity", "medium")
            type_counts[t] = type_counts.get(t, 0) + 1
            severity_counts[s] = severity_counts.get(s, 0) + 1

        content += "SUMMARY BY TYPE:\n"
        for t, count in sorted(type_counts.items()):
            content += f"  - {t.replace('_', ' ').title()}: {count}\n"

        content += "\nSUMMARY BY SEVERITY:\n"
        for s, count in sorted(severity_counts.items()):
            content += f"  - {s.title()}: {count}\n"

        content += "\n" + "=" * 60 + "\n"
        content += "DETAILED RECORDS\n"
        content += "=" * 60 + "\n\n"

        for i, incident in enumerate(incidents, 1):
            content += f"INCIDENT #{i}\n"
            content += "-" * 40 + "\n"
            content += f"Date: {incident.get('date', 'N/A')}\n"
            content += f"Type: {incident.get('type', 'N/A').replace('_', ' ').title()}\n"
            content += f"Severity: {incident.get('severity', 'N/A').title()}\n"
            content += f"Title: {incident.get('title', 'N/A')}\n"
            content += f"Description:\n  {incident.get('description', 'N/A')}\n"

            if incident.get("reported_to"):
                content += f"Reported To: {incident.get('reported_to')}\n"
            if incident.get("witnesses"):
                content += f"Witnesses: {incident.get('witnesses')}\n"
            if incident.get("outcome"):
                content += f"Outcome: {incident.get('outcome')}\n"

            content += f"Logged: {incident.get('created_at', 'N/A')}\n"
            content += "\n"

        content += "=" * 60 + "\n"
        content += "END OF REPORT\n"
        content += "=" * 60 + "\n"

        return StreamingResponse(
            iter([content]),
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename=incident-report-{start_date}-to-{end_date}.txt"}
        )

    else:
        raise HTTPException(status_code=400, detail="Invalid format. Use 'csv' or 'pdf'")
