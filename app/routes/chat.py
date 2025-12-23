"""
Chat API Routes
Handles conversation with the agent
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from app.middleware.auth import get_current_user
from app.database import Database
from app.engines.chat_service import create_chat_service


router = APIRouter(prefix="/chat", tags=["chat"])


class SendMessageRequest(BaseModel):
    content: str
    auto_execute: bool = True  # Execute commands directly without proposal (proposals table has issues)


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    command_id: Optional[str] = None
    created_at: str


@router.post("/message")
async def send_message(
    request: SendMessageRequest,
    user: dict = Depends(get_current_user)
):
    """
    Send a message to the agent and get a response.
    
    If the agent's response contains a command:
    - If auto_execute=True: Command is executed immediately
    - If auto_execute=False: A proposal is created for user approval
    """
    db = Database(use_admin=True)
    chat_service = create_chat_service(db, user["id"])
    
    try:
        result = await chat_service.send_message(
            content=request.content,
            auto_execute=request.auto_execute
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_history(
    limit: int = 50,
    user: dict = Depends(get_current_user)
):
    """Get chat history for the current user"""
    db = Database(use_admin=True)
    chat_service = create_chat_service(db, user["id"])
    
    history = await chat_service.get_history(limit=limit)
    return {"messages": history}


@router.delete("/history")
async def clear_history(
    user: dict = Depends(get_current_user)
):
    """Clear chat history for the current user"""
    db = Database(use_admin=True)
    chat_service = create_chat_service(db, user["id"])
    
    result = await chat_service.clear_history()
    return result
