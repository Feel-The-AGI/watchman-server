"""A saved rotation means the account has finished setup."""

from loguru import logger


async def finish_onboarding_for_saved_cycle(db, user_id: str) -> None:
    """Set onboarding_completed when a rotation is saved and setup is still open.

    A failure here does not undo the cycle write.
    """
    try:
        await db.complete_onboarding_if_open(user_id)
    except Exception as exc:
        logger.warning(f"Could not mark onboarding complete for {user_id}: {exc}")
