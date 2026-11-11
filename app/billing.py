"""Paid-access period calculations.

Checkout creates a one-off Paystack charge in GHS. The amount comes from
PLANS, never from a client-supplied price or a live exchange rate. A
verified charge grants that plan's days. Recurring Paystack plans are a
separate product decision and are intentionally not created here.
"""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


PRO_PERIOD_DAYS = 30
DEFAULT_PLAN_ID = "monthly"

# Fixed cedis. display_usd is for the pricing page only and is never charged.
# founding_yearly stays in the table but is not offered until the cap ships.
_PLAN_ROWS = (
    ("monthly", "70.00", "6", 30, True),
    ("yearly", "560.00", "48", 365, True),
    ("founding_yearly", "420.00", "36", 365, False),
)


class Plan:
    def __init__(self, plan_id: str, amount_ghs: str, display_usd: str, days: int, offered: bool):
        self.id = plan_id
        self.amount_ghs = Decimal(amount_ghs)
        self.display_usd = Decimal(display_usd)
        self.days = days
        self.offered = offered

    def as_public(self) -> dict:
        return {
            "id": self.id,
            "amount_ghs": float(self.amount_ghs),
            "display_usd": float(self.display_usd),
            "days": self.days,
        }


class UnknownPlan(Exception):
    def __init__(self, plan_id: str):
        self.plan_id = plan_id
        super().__init__(plan_id)


class PlanNotOffered(Exception):
    def __init__(self, plan_id: str):
        self.plan_id = plan_id
        super().__init__(plan_id)


PLANS = {
    plan_id: Plan(plan_id, amount, display, days, offered)
    for plan_id, amount, display, days, offered in _PLAN_ROWS
}

_PLAN_IN_TEXT = re.compile(r"plan=([a-z0-9_]+)")


def checkout_plan(plan_id: Optional[str]) -> Plan:
    """The plan a checkout may charge. Missing means the monthly default."""
    key = (plan_id or DEFAULT_PLAN_ID).strip().lower() or DEFAULT_PLAN_ID
    plan = PLANS.get(key)
    if plan is None:
        raise UnknownPlan(key)
    if not plan.offered:
        raise PlanNotOffered(key)
    return plan


def offered_plans() -> list:
    return [plan for plan in PLANS.values() if plan.offered]


def founding_available() -> bool:
    return PLANS["founding_yearly"].offered


def to_pesewas(amount) -> int:
    """Paystack's integer minor units for a GHS amount."""
    return int(
        (Decimal(str(amount)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def checkout_description(plan: Plan) -> str:
    """Human text plus a plan id the grant can read if the plan column is absent."""
    return f"plan={plan.id}; Watchman Pro {plan.id} for {plan.days} days"


def period_days_for_payment(payment: dict) -> int:
    """Days granted by a stored checkout. A legacy row with no plan stays at 30."""
    plan_id = payment.get("plan") if isinstance(payment, dict) else None
    if not plan_id:
        match = _PLAN_IN_TEXT.search(str((payment or {}).get("description") or ""))
        plan_id = match.group(1) if match else None
    plan = PLANS.get(str(plan_id or ""))
    if plan is not None:
        return plan.days
    return PRO_PERIOD_DAYS


def as_utc(value: Optional[datetime] = None) -> datetime:
    """Return an aware UTC timestamp. Naive values are treated as UTC."""
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def parse_timestamp(value) -> Optional[datetime]:
    """Parse an ISO timestamp from the database or Paystack."""
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


def ghs_pesewas(usd_price: float, rate: float) -> tuple[Decimal, int]:
    """Convert a USD price to a 2-decimal GHS amount and Paystack pesewas."""
    ghs = (Decimal(str(usd_price)) * Decimal(str(rate))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    pesewas = int((ghs * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))
    return ghs, pesewas


def charge_matches(amount_local, webhook_amount, currency: Optional[str]) -> bool:
    """True when a Paystack charge matches the server-priced GHS amount."""
    if str(currency or "").upper() != "GHS":
        return False
    if amount_local is None or webhook_amount is None:
        return False
    try:
        expected = int(
            (Decimal(str(amount_local)) * Decimal("100")).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        actual = int(webhook_amount)
    except (ArithmeticError, ValueError):
        return False
    return actual == expected


def is_active_paying_user(user: dict, now: Optional[datetime] = None) -> bool:
    """Pro users with current access. Admin accounts are not paying users."""
    if user.get("tier") != "pro":
        return False
    return not access_expired(user, now)


def paying_user_count(users: list, now: Optional[datetime] = None) -> int:
    return sum(1 for user in users if is_active_paying_user(user, now))


def monthly_recurring_revenue(users: list, price_usd: float, now: Optional[datetime] = None) -> float:
    return paying_user_count(users, now) * price_usd
