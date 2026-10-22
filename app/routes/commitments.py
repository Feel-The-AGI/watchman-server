"""
Watchman Commitments Routes
Endpoints for managing commitments (education, personal, etc.)
"""

import hashlib
import json
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import date
from uuid import UUID, uuid4

from app.commitment_limit import free_commitment_limit_hit
from app.database import Database
from app.engines.command_executor import (
    PENDING_COMMITMENT_ADD,
    PENDING_COMMITMENT_REMOVAL,
    create_command_executor,
)
from app.engines.master_settings_service import MasterSettingsService
from app.middleware.auth import get_current_user, get_effective_tier
from loguru import logger


router = APIRouter()

# Ids removed from the stamped list but not yet rebuilt. A failed rebuild
# leaves the id here so a later calendar read can finish the write.
_PENDING_REMOVAL = PENDING_COMMITMENT_REMOVAL


class CommitmentConstraintsSchema(BaseModel):
    study_on: Optional[List[str]] = None
    exclude: Optional[List[str]] = None
    frequency: Optional[str] = None
    duration_hours: Optional[float] = None


class CreateCommitmentRequest(BaseModel):
    id: Optional[str] = None
    name: str
    type: str  # education, personal, study, sleep
    priority: int = 1
    constraints_json: Optional[dict] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    recurrence: Optional[dict] = None
    total_sessions: Optional[int] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    notes: Optional[str] = None
    status: str = "active"


class UpdateCommitmentRequest(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[int] = None
    constraints_json: Optional[dict] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    recurrence: Optional[dict] = None
    completed_sessions: Optional[int] = None
    color: Optional[str] = None
    notes: Optional[str] = None


@router.get("")
async def list_commitments(
    status: Optional[str] = None,
    type: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    """Get all commitments for the current user"""
    db = Database(use_admin=True)
    commitments = await db.get_commitments(user["id"])
    
    # Filter if needed
    if status:
        commitments = [c for c in commitments if c.get("status") == status]
    
    if type:
        commitments = [c for c in commitments if c.get("type") == type]
    
    return {
        "success": True,
        "data": commitments
    }


@router.get("/active")
async def list_active_commitments(user: dict = Depends(get_current_user)):
    """Get all active commitments"""
    db = Database(use_admin=True)
    commitments = await db.get_active_commitments(user["id"])
    
    return {
        "success": True,
        "data": commitments,
        "count": len(commitments)
    }


@router.get("/{commitment_id}")
async def get_commitment(
    commitment_id: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific commitment"""
    db = Database(use_admin=True)
    commitment = await db.get_commitment(commitment_id)
    
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")
    
    if commitment.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return {
        "success": True,
        "data": commitment
    }


@router.post("")
async def create_commitment(
    data: CreateCommitmentRequest,
    user: dict = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Create a new commitment"""
    db = Database(use_admin=True)
    
    logger.info(f"User {user['id']} creating commitment: {data.name} ({data.type})")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        idempotency_key = None
    supplied_id = _canonical_uuid(data.id)
    fingerprint = _request_fingerprint(data) if idempotency_key else None

    commitment_data = {
        "user_id": user["id"],
        "name": data.name,
        "type": data.type,
        "status": data.status,
        "priority": data.priority,
        "constraints_json": data.constraints_json or {
            "study_on": ["off", "work_day_evening"],
            "exclude": ["work_night"],
            "frequency": "weekly",
            "duration_hours": 2
        },
        "start_date": data.start_date.isoformat() if data.start_date else None,
        "end_date": data.end_date.isoformat() if data.end_date else None,
        "recurrence": data.recurrence,
        "total_sessions": data.total_sessions,
        "color": data.color or "#2979FF",
        "icon": data.icon,
        "notes": data.notes,
        "source": "manual"
    }

    existing_rows = await db.read_commitments(user["id"])
    reused = _reusable_commitment(
        existing_rows, user["id"], supplied_id, idempotency_key, fingerprint
    )
    if reused is None and supplied_id:
        other = await db.get_commitment(supplied_id)
        if other and other.get("user_id") != user["id"]:
            raise HTTPException(status_code=403, detail="Not authorized")
    tier = get_effective_tier(user)
    if free_commitment_limit_hit(tier, existing_rows, reused):
        logger.warning(f"Free tier user {user['id']} blocked from creating additional commitment")
        raise HTTPException(
            status_code=403,
            detail="You've hit the 2 commitment limit on the free plan. Want to track more? Upgrade to Pro for unlimited commitments."
        )
    if data.type == "education" and data.status == "active" and reused is None:
        education_count = sum(
            1 for row in existing_rows
            if row.get("type") == "education" and row.get("status") == "active"
        )
        max_concurrent = user.get("settings", {}).get("max_concurrent_commitments", 2)
        if education_count >= max_concurrent:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum {max_concurrent} concurrent education commitments allowed. Current: {education_count}"
            )

    # POST never overwrites an existing row. The same request is reused only
    # when the client repeats an id or an Idempotency-Key.
    if reused is not None:
        commitment = reused
    else:
        commitment_data["id"] = supplied_id or str(uuid4())
        if idempotency_key:
            commitment_data["source_text"] = f"idempotency:{idempotency_key}:{fingerprint}"
        commitment = await db.create_commitment(commitment_data)
    if not commitment:
        raise HTTPException(status_code=500, detail="Commitment could not be saved")
    outcome = await _publish_commitment(db, user["id"], commitment)
    # A rebuild that already wrote the calendar must not be a 500. The client
    # would retry, and a retry without an Idempotency-Key inserts a second row.
    if outcome == "unrecorded":
        raise HTTPException(status_code=500, detail="Commitment could not be saved")

    logger.info(f"Commitment created: {commitment.get('id')} for user {user['id']}")

    return {
        "success": True,
        "message": "Commitment created",
        "data": commitment,
        "calendar_pending": outcome == "pending",
    }


@router.patch("/{commitment_id}")
@router.put("/{commitment_id}")
async def update_commitment(
    commitment_id: str,
    data: UpdateCommitmentRequest,
    user: dict = Depends(get_current_user)
):
    """Update a commitment"""
    db = Database(use_admin=True)
    
    # Verify ownership
    existing = await db.get_commitment(commitment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Commitment not found")
    if existing.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Check concurrent limit if activating
    if data.status == "active" and existing.get("status") != "active":
        if existing.get("type") == "education":
            stored = await db.read_commitments(user["id"])
            education_count = sum(
                1 for row in stored
                if row.get("type") == "education" and row.get("status") == "active"
            )
            
            settings = user.get("settings", {})
            max_concurrent = settings.get("max_concurrent_commitments", 2)
            
            if education_count >= max_concurrent:
                raise HTTPException(
                    status_code=400,
                    detail=f"Maximum {max_concurrent} concurrent education commitments allowed"
                )
    
    update_data = {}
    
    if data.name is not None:
        update_data["name"] = data.name
    if data.type is not None:
        update_data["type"] = data.type
    if data.status is not None:
        update_data["status"] = data.status
    if data.priority is not None:
        update_data["priority"] = data.priority
    if data.constraints_json is not None:
        update_data["constraints_json"] = data.constraints_json
    if data.start_date is not None:
        update_data["start_date"] = data.start_date.isoformat()
    if data.end_date is not None:
        update_data["end_date"] = data.end_date.isoformat()
    if data.recurrence is not None:
        update_data["recurrence"] = data.recurrence
    if data.completed_sessions is not None:
        update_data["completed_sessions"] = data.completed_sessions
    if data.color is not None:
        update_data["color"] = data.color
    if data.notes is not None:
        update_data["notes"] = data.notes
    
    if not update_data:
        return {"message": "No changes provided"}

    commitment = await db.update_commitment(commitment_id, update_data)
    if not commitment:
        raise HTTPException(status_code=500, detail="Commitment could not be saved")
    saved = {**existing, **commitment}
    outcome = await _publish_commitment(db, user["id"], saved)
    if outcome in ("unrecorded", "visible"):
        raise HTTPException(status_code=500, detail="Commitment could not be saved")

    return {
        "success": True,
        "message": "Commitment updated",
        "data": saved,
        "calendar_pending": outcome == "pending",
    }


@router.delete("/{commitment_id}")
async def delete_commitment(
    commitment_id: str,
    user: dict = Depends(get_current_user)
):
    """Delete a commitment.

    Safe to retry. A missing table row is still removed from the settings
    document, and a failed rebuild stays pending until a later call finishes it.
    """
    db = Database(use_admin=True)
    user_id = user["id"]
    existing = await db.get_commitment(commitment_id)
    if existing and existing.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    service = MasterSettingsService(db)
    current = await service.get(user_id)
    settings = current["settings"] if isinstance(current.get("settings"), dict) else {}
    in_document = _listed(settings.get("commitments"), commitment_id)
    pending = commitment_id in _text_ids(settings.get(_PENDING_REMOVAL))
    if not existing and not in_document and not pending:
        raise HTTPException(status_code=404, detail="Commitment not found")

    try:
        if in_document:
            await service.remove_from_list(user_id, "commitments", commitment_id)
        if not pending:
            await _remember_removal(service, user_id, commitment_id)
    except Exception as exc:
        logger.error(f"[COMMITMENTS] Settings update failed for {commitment_id}: {exc}")
        raise HTTPException(status_code=500, detail="Commitment could not be removed") from exc

    if existing:
        deleted = await db.delete_commitment(commitment_id, user_id)
        if not deleted and await db.get_commitment(commitment_id):
            raise HTTPException(status_code=500, detail="Commitment could not be removed")

    executor = create_command_executor(db, user_id)
    try:
        wrote = await executor._regenerate_calendar()
        if not wrote:
            await executor._project_saved_events()
    except Exception as exc:
        logger.error(f"[COMMITMENTS] Rebuild failed for {commitment_id}: {exc}")
        raise HTTPException(status_code=500, detail="Commitment could not be removed") from exc

    try:
        await _forget_removal(service, user_id, commitment_id)
    except Exception as exc:
        logger.error(f"[COMMITMENTS] Could not clear pending removal for {commitment_id}: {exc}")
        raise HTTPException(status_code=500, detail="Commitment could not be removed") from exc

    return {
        "success": True,
        "message": "Commitment deleted"
    }


def _canonical_uuid(value: Optional[str]) -> Optional[str]:
    """A client-supplied id must be a UUID. None means the server chooses one."""
    if value is None:
        return None
    try:
        return str(UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="Commitment id must be a UUID")


def _request_fingerprint(data: CreateCommitmentRequest) -> str:
    """Stable hash of the create body, so a reused key cannot change the row."""
    payload = data.model_dump(mode="json")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reusable_commitment(
    rows: list,
    user_id: str,
    supplied_id: Optional[str],
    idempotency_key: Optional[str],
    fingerprint: Optional[str],
):
    """An existing row this POST may finish. Never a name or date match."""
    if idempotency_key:
        prefix = f"idempotency:{idempotency_key}:"
        for row in rows or []:
            if not isinstance(row, dict) or row.get("user_id") != user_id:
                continue
            source = row.get("source_text")
            if not isinstance(source, str) or not source.startswith(prefix):
                continue
            digest = source[len(prefix):]
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                continue
            if digest != fingerprint:
                raise HTTPException(
                    status_code=422,
                    detail="Idempotency-Key was already used for a different request",
                )
            return row
    if supplied_id:
        for row in rows or []:
            if isinstance(row, dict) and str(row.get("id") or "") == supplied_id and row.get("user_id") == user_id:
                return row
    return None


async def _publish_commitment(db: Database, user_id: str, saved: dict) -> str:
    """Copy the commitment into settings and rebuild the calendar.

    "unrecorded" means the settings write and the rebuild both failed. Create
    returns 500 so a retry with the same key can finish, and no calendar was
    written for the client to keep. "visible" means the settings copy failed
    and the rebuild succeeded, so the calendar already shows the row.
    "pending" means the id was saved and a later calendar read can finish the
    rebuild. "ready" means it is already on the calendar.
    """
    service = MasterSettingsService(db)
    saved_id = str(saved.get("id") or "")
    recorded = False
    try:
        current = await service.get(user_id)
        settings = dict(current.get("settings") or {})
        items = []
        replaced = False
        for item in settings.get("commitments") or []:
            if isinstance(item, dict) and str(item.get("id") or "") == saved_id:
                items.append(saved)
                replaced = True
            else:
                items.append(item)
        if not replaced:
            items.append(saved)
        settings["commitments"] = items
        pending = [item for item in _text_ids(settings.get(PENDING_COMMITMENT_ADD)) if item != saved_id]
        pending.append(saved_id)
        settings[PENDING_COMMITMENT_ADD] = pending
        await service.update(user_id, settings, current["version"])
        recorded = True
    except Exception as exc:
        logger.error(f"[COMMITMENTS] Settings update failed for {saved_id}: {exc}")

    rebuilt = False
    executor = create_command_executor(db, user_id)
    try:
        wrote = await executor._regenerate_calendar()
        if not wrote:
            await executor._project_saved_events()
        rebuilt = True
    except Exception as exc:
        logger.error(f"[COMMITMENTS] Rebuild failed for {saved_id}: {exc}")

    if not recorded and not rebuilt:
        return "unrecorded"
    if not recorded:
        return "visible"
    if not rebuilt:
        return "pending"

    try:
        fresh = await service.get(user_id)
        cleared = dict(fresh.get("settings") or {})
        cleared[PENDING_COMMITMENT_ADD] = [
            item for item in _text_ids(cleared.get(PENDING_COMMITMENT_ADD)) if item != saved_id
        ]
        await service.update(user_id, cleared, fresh["version"])
    except Exception as exc:
        logger.error(f"[COMMITMENTS] Could not clear pending add for {saved_id}: {exc}")
        return "pending"
    return "ready"


def _listed(items, commitment_id: str) -> bool:
    return any(
        isinstance(item, dict) and str(item.get("id") or "") == commitment_id
        for item in items or []
    )


def _text_ids(items) -> set:
    return {str(item) for item in items or []}


async def _remember_removal(service: MasterSettingsService, user_id: str, commitment_id: str):
    current = await service.get(user_id)
    settings = dict(current["settings"])
    pending = [item for item in _text_ids(settings.get(_PENDING_REMOVAL)) if item != commitment_id]
    pending.append(commitment_id)
    settings[_PENDING_REMOVAL] = pending
    await service.update(user_id, settings, current["version"])


async def _forget_removal(service: MasterSettingsService, user_id: str, commitment_id: str):
    current = await service.get(user_id)
    settings = dict(current["settings"])
    current_ids = _text_ids(settings.get(_PENDING_REMOVAL))
    if commitment_id not in current_ids:
        return
    settings[_PENDING_REMOVAL] = [item for item in current_ids if item != commitment_id]
    await service.update(user_id, settings, current["version"])
