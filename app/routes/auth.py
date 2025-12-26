"""
Watchman Auth Routes
Authentication endpoints
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from loguru import logger

from app.database import Database
from app.middleware.auth import get_current_user


router = APIRouter()


class UserProfileResponse(BaseModel):
    id: str
    email: str
    name: str
    timezone: str
    tier: str
    role: str
    onboarding_completed: bool
    settings: dict


@router.get("/me", response_model=UserProfileResponse)
async def get_profile(user: dict = Depends(get_current_user)):
    """Get the current user's profile"""
    logger.info(f"[AUTH_ROUTE] GET /me - user_id: {user.get('id')}")
    return UserProfileResponse(
        id=user.get("id"),
        email=user.get("email"),
        name=user.get("name"),
        timezone=user.get("timezone", "UTC"),
        tier=user.get("tier", "free"),
        role=user.get("role", "user"),
        onboarding_completed=user.get("onboarding_completed", False),
        settings=user.get("settings", {})
    )


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    timezone: Optional[str] = None


@router.patch("/me")
async def update_profile(
    data: UpdateProfileRequest,
    user: dict = Depends(get_current_user)
):
    """Update the current user's profile"""
    logger.info(f"[AUTH_ROUTE] PATCH /me - user_id: {user.get('id')}, data: {data.model_dump()}")
    db = Database(use_admin=True)

    update_data = {}
    if data.name:
        update_data["name"] = data.name
    if data.timezone:
        update_data["timezone"] = data.timezone

    if not update_data:
        logger.debug(f"[AUTH_ROUTE] No changes provided for user {user.get('id')}")
        return {"message": "No changes provided"}

    updated_user = await db.update_user(user["id"], update_data)
    logger.info(f"[AUTH_ROUTE] Profile updated for user {user.get('id')}")

    return {
        "success": True,
        "message": "Profile updated",
        "data": updated_user
    }


@router.post("/complete-onboarding")
async def complete_onboarding(user: dict = Depends(get_current_user)):
    """Mark the user's onboarding as complete and set up default constraints"""
    logger.info(f"[AUTH_ROUTE] POST /complete-onboarding - user_id: {user.get('id')}")
    db = Database(use_admin=True)

    if user.get("onboarding_completed"):
        logger.debug(f"[AUTH_ROUTE] Onboarding already completed for user {user.get('id')}")
        return {"message": "Onboarding already completed"}

    # Create default constraints
    logger.info(f"[AUTH_ROUTE] Creating default constraints for user {user.get('id')}")
    await db.create_default_constraints(user["id"])

    # Mark onboarding as complete
    await db.complete_onboarding(user["id"])
    logger.info(f"[AUTH_ROUTE] Onboarding completed for user {user.get('id')}")

    return {
        "success": True,
        "message": "Onboarding completed. Default constraints created."
    }
