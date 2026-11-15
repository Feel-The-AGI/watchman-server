"""Shared request-field checks, so bad input is a clean 422 and never a 500
or a stored junk value."""

import math
from datetime import date
from typing import Annotated, Optional
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BeforeValidator

MIN_YEAR = 2000
MAX_YEAR = 2100
MAX_LEAVE_DAYS = 366


def _text(limit: int, allow_empty: bool):
    def check(value):
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError("must be text")
        if "\x00" in value:
            raise ValueError("contains a character that cannot be saved")
        cleaned = value.strip()
        if not allow_empty and not cleaned:
            raise ValueError("cannot be empty")
        if len(cleaned) > limit:
            raise ValueError(f"must be at most {limit} characters")
        return cleaned
    return check


def _sane_date(value: Optional[date]):
    if value is None:
        return value
    if not (MIN_YEAR <= value.year <= MAX_YEAR):
        raise ValueError(f"year must be between {MIN_YEAR} and {MAX_YEAR}")
    return value


def _day_string(value):
    if value is None:
        return value
    if not isinstance(value, str):
        raise ValueError("must be a date like 2026-11-15")
    try:
        parsed = date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise ValueError("is not a real date. Use YYYY-MM-DD")
    _sane_date(parsed)
    return parsed.isoformat()


def _finite(low: float, high: float):
    def check(value):
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        if math.isnan(value) or math.isinf(value):
            raise ValueError("must be a real number")
        if value < low or value > high:
            raise ValueError(f"must be between {low:g} and {high:g}")
        return value
    return check


def _timezone(value):
    if value is None:
        return value
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ValueError("is not a known timezone")
    try:
        ZoneInfo(value.strip())
    except Exception:
        raise ValueError("is not a known timezone")
    return value.strip()


def check_span(start: Optional[date], end: Optional[date], max_days: Optional[int] = None) -> None:
    """End on or after start, and optionally no longer than max_days."""
    if start is None or end is None:
        return
    if end < start:
        raise ValueError("End date must be on or after the start date")
    if max_days is not None and (end - start).days + 1 > max_days:
        raise ValueError(f"A single block can cover at most {max_days} days")


Name = Annotated[str, AfterValidator(_text(120, allow_empty=False))]
ShortText = Annotated[str, AfterValidator(_text(200, allow_empty=False))]
OptionalShortText = Annotated[Optional[str], AfterValidator(_text(200, allow_empty=True))]
LongText = Annotated[str, AfterValidator(_text(5000, allow_empty=False))]
OptionalLongText = Annotated[Optional[str], AfterValidator(_text(5000, allow_empty=True))]
SaneDate = Annotated[date, AfterValidator(_sane_date)]
OptionalSaneDate = Annotated[Optional[date], AfterValidator(_sane_date)]
DayString = Annotated[str, BeforeValidator(_day_string)]
Hours = Annotated[float, BeforeValidator(_finite(0, 24))]
OptionalHours = Annotated[Optional[float], BeforeValidator(_finite(0, 24))]
TimezoneName = Annotated[Optional[str], BeforeValidator(_timezone)]
