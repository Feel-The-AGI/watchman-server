"""Free planning stops on a rolling date a few months from today.

The end date is included. The next day is not. Month ends clamp to the
last real day of the target month, so 30 Nov plus three months is 28 or 29 Feb.
"""

from calendar import monthrange
from datetime import date, datetime
from typing import Iterable, Optional


UNLIMITED_TIERS = {"pro", "admin", "trial"}
FREE_HORIZON_MONTHS = 3


def add_months(day: date, months: int) -> date:
    """Move by whole months and clamp the day when the target month is shorter."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def horizon_end(today: Optional[date] = None) -> date:
    return add_months(today or date.today(), FREE_HORIZON_MONTHS)


def parse_day(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def unlimited(tier: str) -> bool:
    return (tier or "") in UNLIMITED_TIERS


def horizon_detail(today: Optional[date] = None) -> dict:
    end = horizon_end(today)
    return {
        "code": "free_horizon",
        "horizon_end": end.isoformat(),
        "message": (
            f"The free plan covers {FREE_HORIZON_MONTHS} months of planning ahead. "
            "Upgrade to Pro to plan further."
        ),
    }


def horizon_violation(tier: str, dates: Iterable, today: Optional[date] = None) -> Optional[dict]:
    """Detail dict when a Free plan would write past the horizon. None when allowed."""
    if unlimited(tier):
        return None
    end = horizon_end(today)
    for raw in dates:
        day = raw if isinstance(raw, date) and not isinstance(raw, datetime) else parse_day(raw)
        if day is not None and day > end:
            return horizon_detail(today)
    return None


def clip_range(
    start: date,
    end: date,
    tier: str,
    today: Optional[date] = None,
) -> Optional[tuple]:
    """Inclusive range a tier may generate. None when the whole range is past."""
    if unlimited(tier):
        return start, end
    cap = horizon_end(today)
    if start > cap:
        return None
    return start, min(end, cap)


def payload_dates(payload: dict) -> list:
    """Dates a chat or API payload would plan. Anchor dates are not included."""
    payload = payload if isinstance(payload, dict) else {}
    found = []
    sources = [payload]
    schedule = payload.get("schedule")
    if isinstance(schedule, dict):
        sources.append(schedule)
    for source in sources:
        for key in ("start_date", "end_date", "date", "target_date"):
            day = parse_day(source.get(key))
            if day is not None:
                found.append(day)
    return found
