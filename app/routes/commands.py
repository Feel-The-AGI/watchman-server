"""
Commands API Routes
Handles command history, undo, and redo
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime, timezone
from uuid import UUID

from app.middleware.auth import get_current_user
from app.database import Database
from app.engines.command_executor import create_command_executor


router = APIRouter(prefix="/commands", tags=["commands"])


@router.get("")
async def list_commands(
    limit: int = 50,
    status: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    """
    List command history for the current user.
    
    Args:
        limit: Max number of commands to return
        status: Filter by status ('applied', 'undone', 'redone')
    """
    db = Database(use_admin=True)
    
    query = db.client.table("command_log").select("*").eq(
        "user_id", user["id"]
    ).order("created_at", desc=True).limit(limit)
    
    if status:
        query = query.eq("status", status)
    
    result = query.execute()
    
    return {"commands": result.data if result.data else []}


@router.get("/{command_id}")
async def get_command(
    command_id: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific command by ID"""
    db = Database(use_admin=True)
    
    result = db.client.table("command_log").select("*").eq(
        "id", command_id
    ).eq("user_id", user["id"]).execute()
    
    if not result.data or len(result.data) == 0:
        raise HTTPException(status_code=404, detail="Command not found")
    
    return result.data[0]


class ExecuteCommandRequest(BaseModel):
    proposal_id: Optional[UUID] = None
    # Older clients submit these fields when the user clicks Apply. They may
    # identify an existing proposal, but must never become an executable command.
    action: Optional[str] = None
    payload: dict = Field(default_factory=dict)
    explanation: Optional[str] = None


class ReviewProposalRequest(BaseModel):
    decision: Literal["approve", "reject"]


@router.get("/proposals/pending")
async def pending_proposals(user: dict = Depends(get_current_user)):
    db = Database(use_admin=True)
    now = datetime.now(timezone.utc).isoformat()
    result = db.client.table("proposals").select("*").eq(
        "user_id", user["id"]
    ).eq("status", "pending").or_(
        f"expires_at.is.null,expires_at.gt.{now}"
    ).order("created_at").limit(50).execute()
    return {"proposals": result.data or []}


@router.post("/proposals/{proposal_id}/review")
async def review_proposal(
    proposal_id: UUID,
    request: ReviewProposalRequest,
    user: dict = Depends(get_current_user),
):
    """Review an owned proposal; execute only its stored command, once."""
    db = Database(use_admin=True)
    now = datetime.now(timezone.utc).isoformat()
    status = "approved" if request.decision == "approve" else "rejected"
    # Atomic compare-and-set prevents duplicate clicks and concurrent reviews
    # from executing the same proposal twice. Never trust a client command body.
    result = db.client.table("proposals").update({
        "status": status, "reviewed_at": now,
    }).eq("id", str(proposal_id)).eq("user_id", user["id"]).eq(
        "status", "pending"
    ).or_(f"expires_at.is.null,expires_at.gt.{now}").execute()
    if not result.data:
        raise HTTPException(status_code=409, detail="Proposal unavailable or already reviewed")
    proposal = result.data[0]
    if request.decision == "reject":
        return {"success": True, "proposal_id": str(proposal_id), "status": status}
    executor = create_command_executor(db, user["id"])
    # Revalidate against the current calendar and constraints at approval time.
    # If execution fails, do not automatically retry a potentially partial change.
    return await executor.execute(
        proposal["command"], source="api", message_id=proposal.get("message_id")
    )


@router.post("/execute")
async def execute_command(
    request: ExecuteCommandRequest,
    user: dict = Depends(get_current_user)
):
    """
    Compatibility endpoint: approval requires the saved proposal identifier.
    """
    proposal_id = request.proposal_id
    if proposal_id is None:
        if not request.action:
            raise HTTPException(status_code=422, detail="A proposal identifier is required")
        pending = await pending_proposals(user)
        match = next((proposal for proposal in pending["proposals"]
                      if proposal["command"].get("action") == request.action
                      and proposal["command"].get("payload", {}) == request.payload), None)
        if not match:
            raise HTTPException(status_code=409, detail="No matching pending proposal")
        proposal_id = UUID(match["id"])
    return await review_proposal(proposal_id, ReviewProposalRequest(decision="approve"), user)


class UndoRequest(BaseModel):
    command_id: Optional[str] = None  # If not provided, undo last command


@router.post("/undo")
async def undo_command(
    request: UndoRequest = UndoRequest(),
    user: dict = Depends(get_current_user)
):
    """
    Undo the last command or a specific command.
    """
    db = Database(use_admin=True)
    executor = create_command_executor(db, user["id"])
    
    command = {
        "action": "undo",
        "payload": {"command_id": request.command_id} if request.command_id else {},
        "explanation": "Undoing last change"
    }
    
    try:
        result = await executor.execute(command, source="api", skip_validation=True)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RedoRequest(BaseModel):
    command_id: Optional[str] = None  # If not provided, redo last undone


@router.post("/redo")
async def redo_command(
    request: RedoRequest = RedoRequest(),
    user: dict = Depends(get_current_user)
):
    """
    Redo the last undone command or a specific command.
    """
    db = Database(use_admin=True)
    executor = create_command_executor(db, user["id"])
    
    command = {
        "action": "redo",
        "payload": {"command_id": request.command_id} if request.command_id else {},
        "explanation": "Redoing change"
    }
    
    try:
        result = await executor.execute(command, source="api", skip_validation=True)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
