"""
Chat API Routes
Handles conversation with the agent
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from loguru import logger

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
    logger.info(f"[CHAT] POST /message - user_id: {user['id']}")
    logger.info(f"[CHAT] Message content: {request.content[:100]}..." if len(request.content) > 100 else f"[CHAT] Message content: {request.content}")
    logger.debug(f"[CHAT] auto_execute: {request.auto_execute}")

    try:
        db = Database(use_admin=True)
        chat_service = create_chat_service(db, user["id"])

        logger.info(f"[CHAT] Sending message to Gemini for user {user['id']}")
        result = await chat_service.send_message(
            content=request.content,
            auto_execute=request.auto_execute
        )

        logger.info(f"[CHAT] Response received - is_command: {result.get('is_command')}")
        if result.get('is_command'):
            logger.info(f"[CHAT] Command detected: {result.get('command', {}).get('action')}")
        if result.get('execution'):
            logger.info(f"[CHAT] Execution result: success={result.get('execution', {}).get('success')}")

        return result
    except Exception as e:
        logger.error(f"[CHAT] Error for user {user['id']}: {str(e)}")
        logger.exception("[CHAT] Full traceback:")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_history(
    limit: int = 50,
    user: dict = Depends(get_current_user)
):
    """Get chat history for the current user"""
    logger.info(f"[CHAT] GET /history - user_id: {user['id']}, limit: {limit}")
    db = Database(use_admin=True)
    chat_service = create_chat_service(db, user["id"])

    history = await chat_service.get_history(limit=limit)
    logger.info(f"[CHAT] Returning {len(history)} messages for user {user['id']}")
    return {"messages": history}


@router.delete("/history")
async def clear_history(
    user: dict = Depends(get_current_user)
):
    """Clear chat history for the current user"""
    logger.info(f"[CHAT] DELETE /history - user_id: {user['id']}")
    db = Database(use_admin=True)
    chat_service = create_chat_service(db, user["id"])

    result = await chat_service.clear_history()
    logger.info(f"[CHAT] History cleared for user {user['id']}")
    return result
