"""
Chat API Routes
Handles conversation with the agent
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from loguru import logger
from datetime import datetime

from app.middleware.auth import get_current_user, get_effective_tier
from app.database import Database
from app.engines.chat_service import create_chat_service

# Free tier limits
FREE_MESSAGE_LIMIT = 100  # Total messages per month
FREE_HISTORY_LIMIT = 50   # Max history messages to retrieve

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

    Free users: Limited to 100 messages per month
    Pro users: Unlimited messages
    """
    logger.info(f"[CHAT] POST /message - user_id: {user['id']}")
    logger.info(f"[CHAT] Message content: {request.content[:100]}..." if len(request.content) > 100 else f"[CHAT] Message content: {request.content}")
    logger.debug(f"[CHAT] auto_execute: {request.auto_execute}")

    try:
        db = Database(use_admin=True)
        effective_tier = get_effective_tier(user)
        message_count = 0

        # Check message limit for free users (trial and pro get unlimited)
        if effective_tier == "free":
            # Count messages sent this month
            now = datetime.now()
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            message_count_result = db.client.table("chat_messages").select(
                "id", count="exact"
            ).eq(
                "user_id", user["id"]
            ).eq(
                "role", "user"
            ).gte(
                "created_at", month_start.isoformat()
            ).execute()

            message_count = message_count_result.count or 0
            logger.info(f"[CHAT] Free user {user['id']} has sent {message_count}/{FREE_MESSAGE_LIMIT} messages this month")

            if message_count >= FREE_MESSAGE_LIMIT:
                logger.warning(f"[CHAT] Free user {user['id']} hit message limit")
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "message_limit_reached",
                        "message": f"You've used all {FREE_MESSAGE_LIMIT} Watchman messages for this month. Upgrade to Pro for unlimited conversations with Watchman!",
                        "messages_used": message_count,
                        "messages_limit": FREE_MESSAGE_LIMIT,
                        "upgrade_url": "/pricing"
                    }
                )

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

        # Add remaining messages info for free users
        if effective_tier == "free":
            result["messages_remaining"] = max(0, FREE_MESSAGE_LIMIT - message_count - 1)

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CHAT] Error for user {user['id']}: {str(e)}")
        logger.exception("[CHAT] Full traceback:")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_history(
    limit: int = 50,
    user: dict = Depends(get_current_user)
):
    """
    Get chat history for the current user.

    Free users: Limited to last 50 messages
    Pro/Trial users: Unlimited history
    """
    effective_tier = get_effective_tier(user)

    # Enforce history limit for free users (trial gets unlimited)
    if effective_tier == "free":
        limit = min(limit, FREE_HISTORY_LIMIT)

    logger.info(f"[CHAT] GET /history - user_id: {user['id']}, limit: {limit}, tier: {effective_tier}")
    db = Database(use_admin=True)
    chat_service = create_chat_service(db, user["id"])

    history = await chat_service.get_history(limit=limit)
    logger.info(f"[CHAT] Returning {len(history)} messages for user {user['id']}")

    return {
        "messages": history,
        "tier": tier,
        "history_limit": FREE_HISTORY_LIMIT if tier == "free" else None
    }


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
