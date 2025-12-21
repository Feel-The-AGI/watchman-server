"""
Watchman Mutations Routes
Endpoints for mutation management (proposals, approvals, rejections)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from app.database import Database
from app.middleware.auth import get_current_user
from app.engines.mutation_engine import create_mutation_engine
from app.engines.calendar_engine import create_calendar_engine


router = APIRouter()


class ReviewMutationRequest(BaseModel):
    action: str  # approve, reject
    reason: Optional[str] = None
    selected_alternative_id: Optional[str] = None


@router.get("")
async def list_mutations(
    status: Optional[str] = None,
    limit: int = 50,
    user: dict = Depends(get_current_user)
):
    """Get mutations for the current user"""
    db = Database()
    mutations = await db.get_mutations(user["id"], status=status, limit=limit)
    
    return {
        "success": True,
        "data": mutations,
        "count": len(mutations)
    }


@router.get("/pending")
async def list_pending_mutations(user: dict = Depends(get_current_user)):
    """Get pending mutations (proposals awaiting approval)"""
    db = Database()
    mutations = await db.get_pending_mutations(user["id"])
    
    return {
        "success": True,
        "data": mutations,
        "count": len(mutations)
    }


@router.get("/{mutation_id}")
async def get_mutation(
    mutation_id: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific mutation with full details"""
    db = Database()
    mutation = await db.get_mutation(mutation_id)
    
    if not mutation:
        raise HTTPException(status_code=404, detail="Mutation not found")
    
    if mutation.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return {
        "success": True,
        "data": mutation
    }


@router.post("/{mutation_id}/review")
async def review_mutation(
    mutation_id: str,
    data: ReviewMutationRequest,
    user: dict = Depends(get_current_user)
):
    """Review a mutation (approve or reject)"""
    db = Database()
    
    mutation = await db.get_mutation(mutation_id)
    
    if not mutation:
        raise HTTPException(status_code=404, detail="Mutation not found")
    
    if mutation.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if mutation.get("status") != "proposed":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot review mutation with status: {mutation.get('status')}"
        )
    
    if data.action == "approve":
        # Apply the mutation
        engine = create_mutation_engine(user["id"])
        calendar_engine = create_calendar_engine(user["id"])
        
        # Get current calendar state
        proposed_diff = mutation.get("proposed_diff", {})
        scope_start = proposed_diff.get("scope_start") or mutation.get("scope_start")
        scope_end = proposed_diff.get("scope_end") or mutation.get("scope_end")
        
        if scope_start and scope_end:
            current_days = await db.get_calendar_days(user["id"], scope_start, scope_end)
        else:
            # Get full year as fallback
            from datetime import date
            year = date.today().year
            current_days = await db.get_calendar_days(user["id"], f"{year}-01-01", f"{year}-12-31")
        
        # Apply the mutation
        new_state, state_hash = engine.apply_mutation(mutation, current_days)
        
        # Save snapshot of previous state
        previous_hash = calendar_engine.compute_state_hash(current_days)
        await db.create_snapshot({
            "user_id": user["id"],
            "mutation_id": mutation_id,
            "state_hash": previous_hash,
            "snapshot": current_days,
            "reason": "Before mutation approval"
        })
        
        # Update calendar days
        await db.upsert_calendar_days(new_state)
        
        # Update mutation status
        await db.update_mutation(mutation_id, {
            "status": "approved",
            "reviewed_at": datetime.utcnow().isoformat(),
            "applied_at": datetime.utcnow().isoformat(),
            "previous_state_hash": previous_hash,
            "new_state_hash": state_hash
        })
        
        return {
            "success": True,
            "message": "Mutation approved and applied",
            "new_state_hash": state_hash
        }
    
    elif data.action == "reject":
        await db.update_mutation(mutation_id, {
            "status": "rejected",
            "reviewed_at": datetime.utcnow().isoformat(),
            "failure_reasons": {"user_rejection": data.reason or "Rejected by user"}
        })
        
        return {
            "success": True,
            "message": "Mutation rejected"
        }
    
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action: {data.action}. Must be 'approve' or 'reject'"
        )


@router.post("/{mutation_id}/select-alternative")
async def select_alternative(
    mutation_id: str,
    alternative_id: str,
    user: dict = Depends(get_current_user)
):
    """Select an alternative proposal from a failed mutation"""
    db = Database()
    
    mutation = await db.get_mutation(mutation_id)
    
    if not mutation:
        raise HTTPException(status_code=404, detail="Mutation not found")
    
    if mutation.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    alternatives = mutation.get("alternatives", [])
    selected = next((a for a in alternatives if a.get("id") == alternative_id), None)
    
    if not selected:
        raise HTTPException(status_code=404, detail="Alternative not found")
    
    # Create a new mutation from the alternative
    new_mutation_data = {
        "user_id": user["id"],
        "status": "proposed",
        "intent": mutation.get("intent"),
        "proposed_diff": {
            "changes": selected.get("changes", []),
            "summary": selected.get("description")
        },
        "explanation": f"Alternative selected: {selected.get('description')}",
        "is_alternative": True,
        "parent_mutation_id": mutation_id,
        "triggered_by": "user"
    }
    
    new_mutation = await db.create_mutation(new_mutation_data)
    
    return {
        "success": True,
        "message": "Alternative selected. New proposal created.",
        "data": new_mutation
    }


@router.get("/{mutation_id}/undo")
async def get_undo_info(
    mutation_id: str,
    user: dict = Depends(get_current_user)
):
    """Get information about what undoing this mutation would do"""
    db = Database()
    
    mutation = await db.get_mutation(mutation_id)
    
    if not mutation:
        raise HTTPException(status_code=404, detail="Mutation not found")
    
    if mutation.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if mutation.get("status") != "approved":
        raise HTTPException(
            status_code=400,
            detail="Can only undo approved mutations"
        )
    
    # Get the previous snapshot
    previous_hash = mutation.get("previous_state_hash")
    
    if not previous_hash:
        raise HTTPException(
            status_code=400,
            detail="No previous state available for undo"
        )
    
    snapshot = await db.get_snapshot_by_hash(previous_hash)
    
    if not snapshot:
        raise HTTPException(
            status_code=400,
            detail="Previous snapshot not found"
        )
    
    return {
        "success": True,
        "data": {
            "mutation_id": mutation_id,
            "can_undo": True,
            "snapshot_date": snapshot.get("created_at"),
            "affected_days": len(snapshot.get("snapshot", []))
        }
    }


@router.post("/{mutation_id}/undo")
async def undo_mutation(
    mutation_id: str,
    user: dict = Depends(get_current_user)
):
    """Undo an approved mutation by restoring previous state"""
    db = Database()
    
    mutation = await db.get_mutation(mutation_id)
    
    if not mutation:
        raise HTTPException(status_code=404, detail="Mutation not found")
    
    if mutation.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if mutation.get("status") != "approved":
        raise HTTPException(
            status_code=400,
            detail="Can only undo approved mutations"
        )
    
    previous_hash = mutation.get("previous_state_hash")
    
    if not previous_hash:
        raise HTTPException(
            status_code=400,
            detail="No previous state available for undo"
        )
    
    snapshot = await db.get_snapshot_by_hash(previous_hash)
    
    if not snapshot:
        raise HTTPException(
            status_code=400,
            detail="Previous snapshot not found"
        )
    
    # Restore the previous state
    previous_days = snapshot.get("snapshot", [])
    await db.upsert_calendar_days(previous_days)
    
    # Mark the mutation as undone (we'll use rejected status for this)
    await db.update_mutation(mutation_id, {
        "status": "rejected",
        "failure_reasons": {"undone": True, "undone_at": datetime.utcnow().isoformat()}
    })
    
    return {
        "success": True,
        "message": "Mutation undone. Previous state restored.",
        "restored_days": len(previous_days)
    }
