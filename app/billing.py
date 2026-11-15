"""Paid-access period calculations.

Pro is a Stripe subscription. Each paid invoice moves pro_expires_at to the
end of the period it paid for, plus a short grace for a late renewal.
Cancelling stops the next renewal; access already paid for stays.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional


PRO_PERIOD_DAYS = 30


def as_utc(value: Optional[datetime] = None) -> datetime:
    """Return an aware UTC timestamp. Naive values are treated as UTC."""
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def parse_timestamp(value) -> Optional[datetime]:
    """Parse an ISO timestamp from the database."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return as_utc(parsed)


def access_expired(user: dict, now: Optional[datetime] = None) -> bool:
    """True when a Pro user's paid period has ended.

    A missing expiry is a complimentary or not-yet-backfilled grant, not an
    expired one. Admin accounts are never expired by this check.
    """
    if user.get("tier") != "pro":
        return False
    expires = parse_timestamp(user.get("pro_expires_at"))
    if expires is None:
        return False
    return expires <= as_utc(now)


def extend_expiry(current, now: Optional[datetime] = None, days: int = PRO_PERIOD_DAYS) -> datetime:
    """Add one paid period, starting from the later of now and the current expiry."""
    start = as_utc(now)
    current_expiry = parse_timestamp(current)
    base = current_expiry if current_expiry and current_expiry > start else start
    return base + timedelta(days=days)


# A renewal can land a little after the period ends. Access holds until then.
RENEWAL_GRACE = timedelta(days=2)

PLANS = ("monthly", "annual")


def access_until(period_end: datetime) -> datetime:
    """Access for a paid period: its end plus the renewal grace."""
    return as_utc(period_end) + RENEWAL_GRACE


def is_active_paying_user(user: dict, now: Optional[datetime] = None) -> bool:
    """Pro users with current access. Admin accounts are not paying users."""
    if user.get("tier") != "pro":
        return False
    return not access_expired(user, now)


def paying_user_count(users: list, now: Optional[datetime] = None) -> int:
    return sum(1 for user in users if is_active_paying_user(user, now))


def monthly_recurring_revenue(users: list, price_usd: float, now: Optional[datetime] = None) -> float:
    return paying_user_count(users, now) * price_usd
