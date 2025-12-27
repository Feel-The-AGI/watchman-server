"""
Watchman Calendar Sharing Routes
Endpoints for sharing calendar with others (Pro feature)
"""

import secrets
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import date

from app.database import Database
from app.middleware.auth import get_current_user, get_effective_tier
from loguru import logger


router = APIRouter()


class CreateShareRequest(BaseModel):
    name: Optional[str] = "My Shared Calendar"
    show_commitments: bool = False  # Whether to show commitment names
    show_work_types: bool = True    # Whether to show day/night/off


class ShareResponse(BaseModel):
    id: str
    share_code: str
    name: str
    share_url: str
    is_active: bool
    show_commitments: bool
    show_work_types: bool
    created_at: str
    view_count: int


@router.post("")
async def create_share(
    data: CreateShareRequest,
    user: dict = Depends(get_current_user)
):
    """
    Create a new shareable calendar link.
    PRO FEATURE: Only available for Pro users.
    """
    # Check tier - sharing is Pro only (trial users get access too)
    effective_tier = get_effective_tier(user)
    if effective_tier not in ["pro", "admin", "trial"]:
        raise HTTPException(
            status_code=403,
            detail="Calendar sharing is a Pro feature. Upgrade to Pro to share your calendar with others!"
        )

    db = Database(use_admin=True)

    # Generate unique share code
    share_code = secrets.token_urlsafe(12)  # ~16 chars, URL-safe

    share_data = {
        "user_id": user["id"],
        "share_code": share_code,
        "name": data.name or "My Shared Calendar",
        "show_commitments": data.show_commitments,
        "show_work_types": data.show_work_types,
        "is_active": True,
        "view_count": 0
    }

    share = await db.create_calendar_share(share_data)

    if not share:
        raise HTTPException(status_code=500, detail="Failed to create share link")

    return {
        "success": True,
        "message": "Share link created",
        "data": {
            **share,
            "share_url": f"/shared/{share_code}"
        }
    }


@router.get("")
async def list_shares(user: dict = Depends(get_current_user)):
    """Get all share links for current user"""
    db = Database(use_admin=True)
    shares = await db.get_calendar_shares(user["id"])

    # Add share URLs
    for share in shares:
        share["share_url"] = f"/shared/{share['share_code']}"

    return {
        "success": True,
        "data": shares
    }


@router.delete("/{share_id}")
async def revoke_share(
    share_id: str,
    user: dict = Depends(get_current_user)
):
    """Revoke a share link"""
    db = Database(use_admin=True)

    success = await db.revoke_calendar_share(share_id, user["id"])

    if not success:
        raise HTTPException(status_code=400, detail="Failed to revoke share")

    return {
        "success": True,
        "message": "Share link revoked"
    }


@router.get("/public/{share_code}")
async def get_shared_calendar(share_code: str):
    """
    Get a shared calendar by its share code.
    This is a PUBLIC endpoint - no authentication required.
    """
    db = Database(use_admin=True)

    # Get the share record
    share = await db.get_calendar_share_by_code(share_code)

    if not share:
        raise HTTPException(status_code=404, detail="Share not found or has been revoked")

    user_id = share["user_id"]

    # Get user's name for display
    owner_name = await db.get_user_name_by_id(user_id)

    # Get calendar data
    today = date.today()
    year = today.year

    calendar_days = await db.get_calendar_days(
        user_id,
        f"{year}-01-01",
        f"{year}-12-31"
    )

    # Filter data based on share settings
    filtered_days = []
    for day in calendar_days:
        filtered_day = {
            "date": day["date"],
            "cycle_day": day.get("cycle_day"),
        }

        if share.get("show_work_types", True):
            filtered_day["work_type"] = day.get("work_type")
            state = day.get("state_json", {})
            filtered_day["is_leave"] = state.get("is_leave", False)

        if share.get("show_commitments", False):
            state = day.get("state_json", {})
            filtered_day["commitments"] = state.get("commitments", [])

        filtered_days.append(filtered_day)

    # Increment view count (fire and forget)
    try:
        db.client.table("calendar_shares").update({
            "view_count": share.get("view_count", 0) + 1
        }).eq("id", share["id"]).execute()
    except Exception as e:
        logger.warning(f"Failed to update view count: {e}")

    return {
        "success": True,
        "data": {
            "name": share.get("name", "Shared Calendar"),
            "owner_name": owner_name,
            "year": year,
            "days": filtered_days,
            "settings": {
                "show_work_types": share.get("show_work_types", True),
                "show_commitments": share.get("show_commitments", False)
            }
        }
    }
