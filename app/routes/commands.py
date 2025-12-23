"""
Commands API Routes
Handles command history, undo, and redo
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

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
    
    result = await query.execute()
    
    return {"commands": result.data if result.data else []}


@router.get("/{command_id}")
async def get_command(
    command_id: str,
    user: dict = Depends(get_current_user)
):
    """Get a specific command by ID"""
    db = Database(use_admin=True)
    
    result = await db.client.table("command_log").select("*").eq(
        "id", command_id
    ).eq("user_id", user["id"]).execute()
    
    if not result.data or len(result.data) == 0:
        raise HTTPException(status_code=404, detail="Command not found")
    
    return result.data[0]


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
