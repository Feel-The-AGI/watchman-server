"""
Commands API Routes
Handles command history, undo, and redo
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from postgrest.exceptions import APIError
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime, timezone
from uuid import UUID

from app.middleware.auth import get_current_user, get_effective_tier
from app.database import Database
from app.engines.command_executor import create_command_executor
from app.leave_access import LEAVE_PRO_REPLY, leave_blocked


router = APIRouter(prefix="/commands", tags=["commands"])


def unexpired_proposal_filter(now: datetime | None = None) -> str:
    """PostgREST `or` filter for proposals that have not expired.

    Values containing `.` or `+` must be double-quoted. An unquoted ISO
    timestamp is split on the fractional-second dot and the query errors.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    stamp = current.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f'expires_at.is.null,expires_at.gt."{stamp}"'


def _restore_pending_proposal(db: Database, proposal_id: UUID, user_id: str) -> None:
    """Return a claimed proposal to pending when nothing was applied."""
    db.client.table("proposals").update({
        "status": "pending",
        "reviewed_at": None,
    }).eq("id", str(proposal_id)).eq("user_id", user_id).eq("status", "approved").execute()


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


# Production proposal_status is pending, approved, declined, expired.
# The chat client posts decision "reject". "rejected" is not an enum label.
DECISION_APPROVE = {"approve", "approved"}
DECISION_DECLINE = {"reject", "rejected", "decline", "declined"}


def proposal_status_for_decision(decision: str) -> str:
    """Map a review action onto the proposal_status value stored in Postgres."""
    value = (decision or "").strip().lower()
    if value in DECISION_APPROVE:
        return "approved"
    if value in DECISION_DECLINE:
        return "declined"
    raise ValueError(f"Unsupported review decision: {decision}")


class ReviewProposalRequest(BaseModel):
    decision: Literal["approve", "approved", "reject", "rejected", "decline", "declined"]


@router.get("/proposals/pending")
async def pending_proposals(user: dict = Depends(get_current_user)):
    db = Database(use_admin=True)
    result = db.client.table("proposals").select("*").eq(
        "user_id", user["id"]
    ).eq("status", "pending").or_(
        unexpired_proposal_filter()
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
    status = proposal_status_for_decision(request.decision)
    # Atomic compare-and-set prevents duplicate clicks and concurrent reviews
    # from executing the same proposal twice. Never trust a client command body.
    try:
        result = db.client.table("proposals").update({
            "status": status, "reviewed_at": now,
        }).eq("id", str(proposal_id)).eq("user_id", user["id"]).eq(
            "status", "pending"
        ).or_(unexpired_proposal_filter()).execute()
    except APIError as exc:
        logger.error(f"[COMMANDS] Proposal {proposal_id} could not be marked {status}: {exc}")
        raise _review_failed(request.decision) from exc
    if not result.data:
        raise HTTPException(status_code=409, detail="Proposal unavailable or already reviewed")
    proposal = result.data[0]
    if status == "declined":
        return {"success": True, "proposal_id": str(proposal_id), "status": status}
    command = proposal.get("command")
    if not isinstance(command, dict):
        _restore_pending_proposal(db, proposal_id, user["id"])
        raise HTTPException(status_code=409, detail="Stored proposal command is invalid")
    executor = create_command_executor(db, user["id"])
    executor.effective_tier = get_effective_tier(user)
    if isinstance(command, dict) and leave_blocked(
        executor.effective_tier,
        command.get("action") or "",
        command.get("payload") or {},
    ):
        _restore_pending_proposal(db, proposal_id, user["id"])
        raise HTTPException(status_code=403, detail=LEAVE_PRO_REPLY)
    # Revalidate against the current calendar and constraints at approval time.
    # A failed write, including a database error, puts the proposal back to
    # pending and returns an error. The client must not show Done.
    try:
        outcome = await executor.execute(
            command,
            source="api",
            message_id=proposal.get("message_id"),
            proposal_id=str(proposal_id),
        )
    except ValueError:
        _restore_pending_proposal(db, proposal_id, user["id"])
        raise HTTPException(status_code=422, detail="Command could not be applied")
    except Exception as exc:
        logger.exception(f"[COMMANDS] Proposal {proposal_id} failed while saving: {exc}")
        _restore_pending_proposal(db, proposal_id, user["id"])
        raise _review_failed(request.decision) from exc
    if not isinstance(outcome, dict) or outcome.get("success") is False:
        _restore_pending_proposal(db, proposal_id, user["id"])
        if isinstance(outcome, dict) and outcome.get("code") == "commitment_limit":
            raise HTTPException(
                status_code=403,
                detail={"code": "commitment_limit", "message": outcome.get("error")},
            )
        if isinstance(outcome, dict) and outcome.get("code") == "free_horizon":
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "free_horizon",
                    "horizon_end": outcome.get("horizon_end"),
                    "message": outcome.get("error"),
                },
            )
        if isinstance(outcome, dict) and "validation" in outcome:
            raise HTTPException(
                status_code=422,
                detail=outcome.get("error") or "Command failed validation",
            )
        logger.error(
            f"[COMMANDS] Proposal {proposal_id} was not saved: "
            f"{outcome.get('error') if isinstance(outcome, dict) else outcome}"
        )
        raise _review_failed(request.decision)
    return outcome


def _review_failed(decision: str) -> HTTPException:
    try:
        declined = proposal_status_for_decision(decision) == "declined"
    except ValueError:
        declined = False
    if declined:
        detail = "The change could not be declined. It is still waiting for review."
    else:
        detail = "The change could not be saved. It is still waiting for review."
    return HTTPException(status_code=500, detail=detail)


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
        match = next((
            proposal for proposal in pending["proposals"]
            if isinstance(proposal.get("command"), dict)
            and proposal["command"].get("action") == request.action
            and proposal["command"].get("payload", {}) == request.payload
        ), None)
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
        logger.exception(f"[COMMANDS] Undo failed for user {user['id']}: {e}")
        raise HTTPException(status_code=500, detail="That action could not be completed. Please try again.")


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
        logger.exception(f"[COMMANDS] Redo failed for user {user['id']}: {e}")
        raise HTTPException(status_code=500, detail="That action could not be completed. Please try again.")
