"""
Watchman Commitments Routes
Endpoints for managing commitments (education, personal, etc.)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import date

from app.database import Database
from app.engines.command_executor import PENDING_COMMITMENT_REMOVAL, create_command_executor
from app.engines.master_settings_service import MasterSettingsService
from app.middleware.auth import get_current_user
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
    user: dict = Depends(get_current_user)
):
    """Create a new commitment"""
    db = Database(use_admin=True)
    
    logger.info(f"User {user['id']} creating commitment: {data.name} ({data.type})")
    
    # Check tier limits for free users
    tier = user.get("tier", "free")
    if tier == "free":
        all_commitments = await db.get_commitments(user["id"])
        if len(all_commitments) >= 2:
            logger.warning(f"Free tier user {user['id']} blocked from creating additional commitment")
            raise HTTPException(
                status_code=403,
                detail="You've hit the 2 commitment limit on the free plan. Want to track more? Upgrade to Pro for unlimited commitments."
            )
    
    # Check concurrent commitment limit for education
    if data.type == "education" and data.status == "active":
        active_education = await db.get_active_commitments(user["id"])
        education_count = sum(1 for c in active_education if c.get("type") == "education")
        
        settings = user.get("settings", {})
        max_concurrent = settings.get("max_concurrent_commitments", 2)
        
        if education_count >= max_concurrent:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum {max_concurrent} concurrent education commitments allowed. Current: {education_count}"
            )
    
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
    
    commitment = await db.create_commitment(commitment_data)
    
    logger.info(f"Commitment created: {commitment.get('id')} for user {user['id']}")
    
    return {
        "success": True,
        "message": "Commitment created",
        "data": commitment
    }


@router.patch("/{commitment_id}")
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
            active_education = await db.get_active_commitments(user["id"])
            education_count = sum(1 for c in active_education if c.get("type") == "education")
            
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
    
    return {
        "success": True,
        "message": "Commitment updated",
        "data": commitment
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
