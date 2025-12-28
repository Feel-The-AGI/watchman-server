"""
Watchman Cron Routes
Endpoints for scheduled tasks (weekly summaries, reminders)
Called by external cron service (cron-job.org) or Render cron jobs
"""

from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Header
from loguru import logger

from app.config import get_settings
from app.database import Database
from app.services.email_service import get_email_service


router = APIRouter()
settings = get_settings()

# Simple secret key for cron endpoints (set in env)
CRON_SECRET = settings.supabase_service_key[:32] if settings.supabase_service_key else "dev-cron-secret"


def verify_cron_secret(x_cron_secret: str = Header(None)):
    """Verify the cron secret to prevent unauthorized access"""
    if x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


@router.post("/weekly-summary")
async def send_weekly_summaries(x_cron_secret: str = Header(None)):
    """
    Send weekly summary emails to all users with email notifications enabled.
    Should be called once per week (e.g., Sunday evening).

    Headers:
        X-Cron-Secret: The cron secret key for authentication
    """
    verify_cron_secret(x_cron_secret)

    logger.info("[CRON] Starting weekly summary job")

    db = Database(use_admin=True)
    email_service = get_email_service()

    if not email_service.enabled:
        logger.warning("[CRON] Email service not enabled, skipping weekly summaries")
        return {"status": "skipped", "reason": "Email service not configured"}

    # Get all users with email notifications enabled
    try:
        result = db.client.table("users").select("id, email, name, settings").execute()
        users = result.data if result.data else []
    except Exception as e:
        logger.error(f"[CRON] Failed to fetch users: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch users")

    # Calculate week range
    today = datetime.now()
    week_start = (today - timedelta(days=today.weekday() + 7)).strftime("%Y-%m-%d")
    week_end = (today - timedelta(days=today.weekday() + 1)).strftime("%Y-%m-%d")

    sent_count = 0
    error_count = 0

    for user in users:
        user_settings = user.get("settings", {})

        # Check if user has email notifications enabled
        if not user_settings.get("notifications_email", False):
            continue

        user_id = user.get("id")
        user_email = user.get("email")
        user_name = user.get("name") or user_email.split("@")[0] if user_email else "there"

        if not user_email:
            continue

        try:
            # Get user's weekly stats
            stats = await get_user_weekly_stats(db, user_id, week_start, week_end)

            # Send the email
            success = await email_service.send_weekly_summary(
                to=user_email,
                user_name=user_name,
                week_start=week_start,
                week_end=week_end,
                stats=stats,
            )

            if success:
                sent_count += 1
                logger.debug(f"[CRON] Weekly summary sent to {user_email}")
            else:
                error_count += 1

        except Exception as e:
            error_count += 1
            logger.error(f"[CRON] Failed to send summary to {user_email}: {e}")

    logger.info(f"[CRON] Weekly summary job complete: {sent_count} sent, {error_count} errors")

    return {
        "status": "completed",
        "sent": sent_count,
        "errors": error_count,
        "week": f"{week_start} to {week_end}"
    }


async def get_user_weekly_stats(db: Database, user_id: str, start_date: str, end_date: str) -> dict:
    """Get user's stats for the week"""
    stats = {
        "work_days": 0,
        "off_days": 0,
        "commitments_completed": 0,
        "incidents": 0,
    }

    try:
        # Get calendar days for the week
        calendar_result = db.client.table("calendar_days").select("work_type").eq(
            "user_id", user_id
        ).gte("date", start_date).lte("date", end_date).execute()

        if calendar_result.data:
            for day in calendar_result.data:
                work_type = day.get("work_type", "")
                if work_type in ["work_day", "work_night"]:
                    stats["work_days"] += 1
                elif work_type == "off":
                    stats["off_days"] += 1

        # Get incidents for the week
        incidents_result = db.client.table("incidents").select("id").eq(
            "user_id", user_id
        ).gte("date", start_date).lte("date", end_date).execute()

        if incidents_result.data:
            stats["incidents"] = len(incidents_result.data)

        # Get completed commitments (simplified - just count total)
        commitments_result = db.client.table("commitments").select("id").eq(
            "user_id", user_id
        ).execute()

        if commitments_result.data:
            stats["commitments_completed"] = len(commitments_result.data)

    except Exception as e:
        logger.error(f"[CRON] Error fetching stats for user {user_id}: {e}")

    return stats


@router.get("/health")
async def cron_health():
    """Health check for cron service"""
    return {
        "status": "ok",
        "service": "watchman-cron",
        "timestamp": datetime.now().isoformat()
    }
