"""
Watchman Settings Routes
Endpoints for user settings and preferences
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.database import Database
from app.middleware.auth import get_current_user, require_admin, get_effective_tier, is_in_trial, TRIAL_DURATION_DAYS
from app.services.email_service import get_email_service


router = APIRouter()


class UpdateSettingsRequest(BaseModel):
    constraint_mode: Optional[str] = None  # binary, weighted
    weighted_mode_enabled: Optional[bool] = None
    max_concurrent_commitments: Optional[int] = None
    notifications_email: Optional[bool] = None
    notifications_push: Optional[bool] = None  # Push notifications
    notifications_whatsapp: Optional[bool] = None  # WhatsApp notifications (legacy)
    theme: Optional[str] = None  # dark, light, system
    timezone: Optional[str] = None  # User timezone


class ConstraintRequest(BaseModel):
    name: str
    description: Optional[str] = None
    rule: dict
    weight: int = 100
    is_active: bool = True


@router.get("")
async def get_settings(user: dict = Depends(get_current_user)):
    """Get all user settings"""
    from datetime import datetime, timedelta, timezone

    actual_tier = user.get("tier", "free")
    effective_tier = get_effective_tier(user)
    in_trial = is_in_trial(user)

    # Calculate trial end date if in trial
    trial_ends_at = None
    if in_trial and user.get("created_at"):
        created_at = user.get("created_at")
        if isinstance(created_at, str):
            created_at = created_at.replace('Z', '+00:00')
            created_date = datetime.fromisoformat(created_at)
        else:
            created_date = created_at
        trial_ends_at = (created_date + timedelta(days=TRIAL_DURATION_DAYS)).isoformat()

    return {
        "success": True,
        "data": {
            "settings": user.get("settings", {}),
            "tier": actual_tier,
            "effective_tier": effective_tier,
            "role": user.get("role", "user"),
            "trial": {
                "active": in_trial,
                "ends_at": trial_ends_at,
                "duration_days": TRIAL_DURATION_DAYS
            } if in_trial else None
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
        # Check tier for weighted mode (trial users get access)
        effective_tier = get_effective_tier(user)
        if data.constraint_mode == "weighted" and effective_tier not in ["pro", "admin", "trial"]:
            raise HTTPException(
                status_code=403,
                detail="Weighted constraints are a Pro feature. Upgrade to unlock advanced scheduling with priorities!"
            )
        current_settings["constraint_mode"] = data.constraint_mode

    if data.weighted_mode_enabled is not None:
        # Check tier for weighted mode (trial users get access)
        effective_tier = get_effective_tier(user)
        if data.weighted_mode_enabled and effective_tier not in ["pro", "admin", "trial"]:
            raise HTTPException(
                status_code=403,
                detail="Weighted constraints are a Pro feature. Upgrade to unlock!"
            )
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

    if data.notifications_push is not None:
        current_settings["notifications_push"] = data.notifications_push

    if data.notifications_whatsapp is not None:
        current_settings["notifications_whatsapp"] = data.notifications_whatsapp

    if data.theme is not None:
        if data.theme not in ["dark", "light", "system"]:
            raise HTTPException(
                status_code=400,
                detail="theme must be 'dark', 'light', or 'system'"
            )
        current_settings["theme"] = data.theme

    if data.timezone is not None:
        current_settings["timezone"] = data.timezone

    await db.update_user(user["id"], {"settings": current_settings})
    
    return {
        "success": True,
        "message": "Settings updated",
        "data": current_settings
    }


@router.get("/constraints")
async def list_constraints(user: dict = Depends(get_current_user)):
    """Get all constraints"""
    db = Database(use_admin=True)
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
    db = Database(use_admin=True)
    
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
    db = Database(use_admin=True)
    
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
    db = Database(use_admin=True)
    
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
    """
    Toggle weighted constraints mode.
    PRO FEATURE: Only Pro or trial users can enable weighted constraints.
    """
    db = Database(use_admin=True)

    # Check tier for enabling weighted mode (trial users get access)
    effective_tier = get_effective_tier(user)
    if enabled and effective_tier not in ["pro", "admin", "trial"]:
        raise HTTPException(
            status_code=403,
            detail="Weighted constraints are a Pro feature. Upgrade to Pro to unlock advanced constraint rules with priorities and weights!"
        )

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
    db = Database(use_admin=True)
    subscription = await db.get_subscription(user["id"])
    
    return {
        "success": True,
        "data": {
            "tier": user.get("tier", "free"),
            "subscription": subscription
        }
    }


@router.delete("/delete-account")
async def delete_account(user: dict = Depends(get_current_user)):
    """
    Permanently delete user account and all associated data.
    This action cannot be undone.
    """
    from loguru import logger
    
    db = Database(use_admin=True)
    
    user_id = user["id"]
    auth_id = user.get("auth_id")
    
    logger.warning(f"Account deletion requested for user {user_id} (auth: {auth_id})")
    
    # Step 1: Delete all user data from public tables
    deleted_summary = await db.delete_all_user_data(user_id)
    logger.info(f"Deleted user data: {deleted_summary}")
    
    # Step 2: Delete from Supabase Auth
    auth_deleted = False
    if auth_id:
        auth_deleted = await db.delete_auth_user(auth_id)
        if auth_deleted:
            logger.info(f"Deleted auth user {auth_id}")
        else:
            logger.error(f"Failed to delete auth user {auth_id}")
    
    return {
        "success": True,
        "message": "Account deleted successfully",
        "deleted": deleted_summary,
        "auth_deleted": auth_deleted
    }


@router.post("/test-email")
async def send_test_email(user: dict = Depends(get_current_user)):
    """
    Send a test email to verify email notifications are working.
    """
    email_service = get_email_service()

    if not email_service.enabled:
        raise HTTPException(
            status_code=503,
            detail="Email service is not configured. Contact support."
        )

    user_email = user.get("email")
    if not user_email:
        raise HTTPException(
            status_code=400,
            detail="No email address found for your account."
        )

    user_name = user.get("name") or user_email.split("@")[0]

    # Send a test email
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0f; color: #e5e5e5; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: #1a1a2e; border-radius: 16px; padding: 32px; }}
            .header {{ text-align: center; margin-bottom: 24px; }}
            .logo {{ font-size: 24px; font-weight: bold; color: #6366f1; }}
            .success {{ background: #10b98120; border: 1px solid #10b98140; padding: 16px; border-radius: 12px; margin: 24px 0; }}
            .success-icon {{ font-size: 48px; text-align: center; }}
            .content {{ line-height: 1.6; text-align: center; }}
            .footer {{ margin-top: 32px; text-align: center; color: #6b7280; font-size: 12px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo">Watchman</div>
            </div>
            <div class="content">
                <div class="success">
                    <div class="success-icon">&#10003;</div>
                    <p style="margin: 8px 0 0 0; font-weight: 600; color: #10b981;">Email notifications are working!</p>
                </div>
                <p>Hi {user_name},</p>
                <p>This is a test email to confirm your email notifications are properly configured.</p>
                <p>You'll receive emails for:</p>
                <ul style="text-align: left; display: inline-block;">
                    <li>Incident alerts</li>
                    <li>Schedule reminders</li>
                    <li>Weekly summaries</li>
                </ul>
            </div>
            <div class="footer">
                <p>Manage your preferences at <a href="https://trywatchman.app/dashboard/settings" style="color: #6366f1;">trywatchman.app</a></p>
            </div>
        </div>
    </body>
    </html>
    """

    success = await email_service.send_email(
        to=user_email,
        subject="Test Email - Watchman Notifications",
        html=html,
    )

    if success:
        return {
            "success": True,
            "message": f"Test email sent to {user_email}"
        }
    else:
        raise HTTPException(
            status_code=500,
            detail="Failed to send test email. Please try again later."
        )
