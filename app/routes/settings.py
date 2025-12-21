"""
Watchman Settings Routes
Endpoints for user settings and preferences
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.database import Database
from app.middleware.auth import get_current_user, require_admin


router = APIRouter()


class UpdateSettingsRequest(BaseModel):
    constraint_mode: Optional[str] = None  # binary, weighted
    weighted_mode_enabled: Optional[bool] = None
    max_concurrent_commitments: Optional[int] = None
    notifications_email: Optional[bool] = None
    notifications_whatsapp: Optional[bool] = None
    theme: Optional[str] = None  # dark, light


class ConstraintRequest(BaseModel):
    name: str
    description: Optional[str] = None
    rule: dict
    weight: int = 100
    is_active: bool = True


@router.get("")
async def get_settings(user: dict = Depends(get_current_user)):
    """Get all user settings"""
    return {
        "success": True,
        "data": {
            "settings": user.get("settings", {}),
            "tier": user.get("tier", "free"),
            "role": user.get("role", "user")
        }
    }


@router.patch("")
async def update_settings(
    data: UpdateSettingsRequest,
    user: dict = Depends(get_current_user)
):
    """Update user settings"""
    db = Database(use_admin=True)
    
    current_settings = user.get("settings", {})
    
    if data.constraint_mode is not None:
        if data.constraint_mode not in ["binary", "weighted"]:
            raise HTTPException(
                status_code=400,
                detail="constraint_mode must be 'binary' or 'weighted'"
            )
        current_settings["constraint_mode"] = data.constraint_mode
    
    if data.weighted_mode_enabled is not None:
        current_settings["weighted_mode_enabled"] = data.weighted_mode_enabled
    
    if data.max_concurrent_commitments is not None:
        if data.max_concurrent_commitments < 1 or data.max_concurrent_commitments > 10:
            raise HTTPException(
                status_code=400,
                detail="max_concurrent_commitments must be between 1 and 10"
            )
        current_settings["max_concurrent_commitments"] = data.max_concurrent_commitments
    
    if data.notifications_email is not None:
        current_settings["notifications_email"] = data.notifications_email
    
    if data.notifications_whatsapp is not None:
        current_settings["notifications_whatsapp"] = data.notifications_whatsapp
    
    if data.theme is not None:
        if data.theme not in ["dark", "light"]:
            raise HTTPException(
                status_code=400,
                detail="theme must be 'dark' or 'light'"
            )
        current_settings["theme"] = data.theme
    
    await db.update_user(user["id"], {"settings": current_settings})
    
    return {
        "success": True,
        "message": "Settings updated",
        "data": current_settings
    }


@router.get("/constraints")
async def list_constraints(user: dict = Depends(get_current_user)):
    """Get all constraints"""
    db = Database()
    constraints = await db.get_constraints(user["id"])
    
    return {
        "success": True,
        "data": constraints
    }


@router.post("/constraints")
async def create_constraint(
    data: ConstraintRequest,
    user: dict = Depends(get_current_user)
):
    """Create a new custom constraint"""
    db = Database()
    
    constraint_data = {
        "user_id": user["id"],
        "name": data.name,
        "description": data.description,
        "rule": data.rule,
        "weight": data.weight,
        "is_active": data.is_active,
        "is_system": False
    }
    
    constraint = await db.create_constraint(constraint_data)
    
    return {
        "success": True,
        "message": "Constraint created",
        "data": constraint
    }


@router.patch("/constraints/{constraint_id}")
async def update_constraint(
    constraint_id: str,
    data: ConstraintRequest,
    user: dict = Depends(get_current_user)
):
    """Update a constraint"""
    db = Database()
    
    update_data = {
        "name": data.name,
        "description": data.description,
        "rule": data.rule,
        "weight": data.weight,
        "is_active": data.is_active
    }
    
    constraint = await db.update_constraint(constraint_id, update_data)
    
    return {
        "success": True,
        "message": "Constraint updated",
        "data": constraint
    }


@router.delete("/constraints/{constraint_id}")
async def delete_constraint(
    constraint_id: str,
    user: dict = Depends(get_current_user)
):
    """Delete a constraint"""
    db = Database()
    
    # Check if it's a system constraint
    constraints = await db.get_constraints(user["id"])
    constraint = next((c for c in constraints if c["id"] == constraint_id), None)
    
    if constraint and constraint.get("is_system"):
        raise HTTPException(
            status_code=400,
            detail="Cannot delete system constraints. You can deactivate them instead."
        )
    
    await db.delete_constraint(constraint_id)
    
    return {
        "success": True,
        "message": "Constraint deleted"
    }


@router.post("/toggle-weighted-mode")
async def toggle_weighted_mode(
    enabled: bool,
    user: dict = Depends(get_current_user)
):
    """Toggle weighted constraints mode"""
    db = Database(use_admin=True)
    
    settings = user.get("settings", {})
    settings["weighted_mode_enabled"] = enabled
    
    if enabled:
        settings["constraint_mode"] = "weighted"
    else:
        settings["constraint_mode"] = "binary"
    
    await db.update_user(user["id"], {"settings": settings})
    
    return {
        "success": True,
        "message": f"Weighted mode {'enabled' if enabled else 'disabled'}",
        "weighted_mode_enabled": enabled
    }


# Admin endpoints

@router.post("/admin/grant-tier")
async def grant_tier(
    user_email: str,
    tier: str,
    admin: dict = Depends(require_admin)
):
    """Grant a tier to a user (admin only)"""
    if tier not in ["free", "pro", "admin"]:
        raise HTTPException(
            status_code=400,
            detail="tier must be 'free', 'pro', or 'admin'"
        )
    
    db = Database(use_admin=True)
    
    # Find user by email
    result = db.client.table("users").select("*").eq("email", user_email).single().execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="User not found")
    
    target_user = result.data
    
    # Update tier
    await db.update_user(target_user["id"], {"tier": tier})
    
    return {
        "success": True,
        "message": f"Granted {tier} tier to {user_email}"
    }


@router.get("/subscription")
async def get_subscription(user: dict = Depends(get_current_user)):
    """Get user's subscription details"""
    db = Database()
    subscription = await db.get_subscription(user["id"])
    
    return {
        "success": True,
        "data": {
            "tier": user.get("tier", "free"),
            "subscription": subscription
        }
    }
