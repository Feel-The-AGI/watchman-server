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

        elapsed = (time.time() - start_time) * 1000
        logger.info(f"[INCIDENTS] Text export complete: {len(incidents)} entries ({elapsed:.2f}ms)")

        return StreamingResponse(
            iter([content]),
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename=incident-report-{start_date}-to-{end_date}.txt"}
        )

    else:
        logger.warning(f"[INCIDENTS] Invalid export format requested: {format}")
        raise HTTPException(status_code=400, detail="Invalid format. Use 'csv' or 'pdf'")
