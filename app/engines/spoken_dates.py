"""Dates people say in chat, resolved from their local today.

A year in the text is used as written. Without one, past tense uses the most
recent date on or before today, and a future request uses the next one.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_ORD = r"(?:st|nd|rd|th)?"
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_WEEKDAY = (
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue(?:s)?|wed|thu(?:r(?:s)?)?|fri|sat|sun"
)
_WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MO = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_RANGE_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}}){_ORD}\s*(?:-|–|—|to|through|thru)\s*(\d{{1,2}}){_ORD}\s+"
    rf"(?:of\s+)?({_MONTH})\.?(?:\s*,?\s*(\d{{4}}))?\b",
    re.IGNORECASE,
)
_RANGE_MONTH_DAY = re.compile(
    rf"\b({_MONTH})\.?\s+(\d{{1,2}}){_ORD}\s*(?:-|–|—|to|through|thru)\s*"
    rf"(\d{{1,2}}){_ORD}(?:\s*,?\s*(\d{{4}}))?\b",
    re.IGNORECASE,
)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b")
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH})\.?(?:\s*,?\s*(\d{{4}}))?\b",
    re.IGNORECASE,
)
_MONTH_DAY = re.compile(
    rf"\b({_MONTH})\.?\s+(\d{{1,2}}){_ORD}(?:\s*,)?(?:\s+(\d{{4}}))?\b",
    re.IGNORECASE,
)
_REL_WEEK = re.compile(rf"\b(last|this|next)\s+({_WEEKDAY})\b", re.IGNORECASE)
_ON_WEEK = re.compile(rf"\b(?:on\s+)?({_WEEKDAY})\b", re.IGNORECASE)
_RELATIVE = re.compile(r"\byesterday\b|\btoday\b|\btomorrow\b", re.IGNORECASE)
_BARE_DAY = re.compile(rf"\b(?:the\s+)?(\d{{1,2}})(st|nd|rd|th)\b", re.IGNORECASE)
_CONNECTOR = re.compile(r"\b(?:to|through|thru|and)\b|[-–—]", re.IGNORECASE)


class BadDate(Exception):
    def __init__(self, label: str):
        self.label = label
        super().__init__(label)


class AmbiguousDate(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


@dataclass
class SpokenSpan:
    start: Optional[date] = None
    end: Optional[date] = None
    problem: Optional[str] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None and self.start is not None


def speak(day: date) -> str:
    """Thu 15 Oct 2026."""
    return f"{_WD[day.weekday()]} {day.day} {_MO[day.month - 1]} {day.year}"


def parse_spoken_span(text: str, today: date, direction: str = "nearest") -> SpokenSpan:
    """One date or an inclusive range. Invalid and ambiguous dates do not guess."""
    if not text or today is None:
        return SpokenSpan(problem="missing", detail="Which day did you mean?")
    try:
        found = _collect(text, today, direction)
    except AmbiguousDate as exc:
        return SpokenSpan(problem="ambiguous", detail=exc.detail)
    except BadDate as exc:
        label = exc.label.strip() or "That"
        if label.lower().startswith("did you mean"):
            return SpokenSpan(problem="ambiguous", detail=label)
        return SpokenSpan(
            problem="invalid",
            detail=f"{label} isn't a real date. Which day did you mean?",
        )
    if not found:
        return SpokenSpan(problem="missing", detail="Which day did you mean?")
    if len(found) == 1:
        start, end = found[0][2], found[0][3]
        return _ordered(start, end)
    if len(found) == 2 and _connector_between(text, found[0][1], found[1][0]):
        start = min(found[0][2], found[1][2])
        end = max(found[0][3], found[1][3])
        if end < start:
            return SpokenSpan(problem="invalid", detail="That range runs backwards. Which days did you mean?")
        return SpokenSpan(start=start, end=end)
    return SpokenSpan(problem="ambiguous", detail="Which day did you mean?")


def relative_dates(text: str, today: date) -> list:
    """Dates named by yesterday, a weekday, or a calendar day. Empty when unclear."""
    span = parse_spoken_span(text, today, direction="nearest")
    if not span.ok:
        return []
    if span.end and span.end != span.start:
        return [span.start, span.end]
    return [span.start]


def _ordered(start: date, end: date) -> SpokenSpan:
    if end < start:
        try:
            end = end.replace(year=end.year + 1)
        except ValueError:
            return SpokenSpan(problem="invalid", detail="That range isn't a real set of dates. Which days did you mean?")
    if end < start:
        return SpokenSpan(problem="invalid", detail="That range runs backwards. Which days did you mean?")
    return SpokenSpan(start=start, end=end)


def _collect(text: str, today: date, direction: str) -> list:
    taken = []
    found = []

    def keep(match, start: date, end: Optional[date] = None):
        span = match.span()
        if _overlaps(span, taken):
            return
        taken.append(span)
        found.append((span[0], span[1], start, end or start))

    for match in _RANGE_DAY_MONTH.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        month = _month_number(match.group(3))
        year = _year(match.group(4))
        start = _resolve(month, int(match.group(1)), year, today, direction, match.group(0))
        end = _resolve(month, int(match.group(2)), year, today, direction, match.group(0))
        keep(match, start, end)
    for match in _RANGE_MONTH_DAY.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        month = _month_number(match.group(1))
        year = _year(match.group(4))
        start = _resolve(month, int(match.group(2)), year, today, direction, match.group(0))
        end = _resolve(month, int(match.group(3)), year, today, direction, match.group(0))
        keep(match, start, end)
    for match in _ISO.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        built = _exact(int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(0))
        keep(match, built)
    for match in _SLASH.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        built = _slash(match, today, direction)
        keep(match, built[0], built[1] if len(built) > 1 else None)
    for match in _DAY_MONTH.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        built = _resolve(
            _month_number(match.group(2)), int(match.group(1)), _year(match.group(3)),
            today, direction, match.group(0),
        )
        keep(match, built)
    for match in _MONTH_DAY.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        built = _resolve(
            _month_number(match.group(1)), int(match.group(2)), _year(match.group(3)),
            today, direction, match.group(0),
        )
        keep(match, built)
    for match in _REL_WEEK.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        keep(match, _relative_weekday(match.group(1), match.group(2), today))
    for match in _RELATIVE.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        word = match.group(0).lower()
        if word == "yesterday":
            keep(match, today - timedelta(days=1))
        elif word == "tomorrow":
            keep(match, today + timedelta(days=1))
        else:
            keep(match, today)
    for match in _ON_WEEK.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        keep(match, _upcoming_weekday(_weekday_index(match.group(1)), today))
    for match in _BARE_DAY.finditer(text):
        if _overlaps(match.span(), taken):
            continue
        built = _resolve(today.month, int(match.group(1)), None, today, direction, match.group(0))
        keep(match, built)
    found.sort(key=lambda item: item[0])
    return found


def _slash(match, today: date, direction: str):
    first = int(match.group(1))
    second = int(match.group(2))
    year = _year(match.group(3))
    label = match.group(0)
    day_month = _try_resolve(second, first, year, today, direction)
    month_day = _try_resolve(first, second, year, today, direction)
    if day_month and month_day and day_month != month_day:
        raise AmbiguousDate(f"Did you mean {speak(day_month)} or {speak(month_day)}?")
    if day_month:
        return (day_month,)
    if month_day:
        return (month_day,)
    raise BadDate(label)


def _try_resolve(month: int, day: int, year: Optional[int], today: date, direction: str):
    if month < 1 or month > 12 or day < 1 or day > 31:
        return None
    try:
        return _resolve(month, day, year, today, direction, "")
    except BadDate:
        return None


def _resolve(month: int, day: int, year: Optional[int], today: date, direction: str, label: str) -> date:
    if year is not None:
        return _exact(year, month, day, label or f"{day} {_MO[month - 1]} {year}")
    options = []
    for candidate in (today.year - 1, today.year, today.year + 1):
        try:
            options.append(date(candidate, month, day))
        except ValueError:
            continue
    if not options:
        shown = label.strip() or f"{day} {_MO[month - 1]}"
        raise BadDate(shown)
    if direction == "past":
        past = [item for item in options if item <= today]
        if not past:
            raise BadDate(label.strip() or f"{day} {_MO[month - 1]}")
        return max(past)
    if direction == "future":
        future = [item for item in options if item >= today]
        if not future:
            raise BadDate(label.strip() or f"{day} {_MO[month - 1]}")
        return min(future)
    return min(options, key=lambda item: (abs((item - today).days), item.toordinal()))


def _exact(year: int, month: int, day: int, label: str) -> date:
    if month < 1 or month > 12:
        raise BadDate(label)
    last = calendar.monthrange(year, month)[1]
    if day < 1 or day > last:
        raise BadDate(label)
    return date(year, month, day)


def _relative_weekday(which: str, name: str, today: date) -> date:
    index = _weekday_index(name)
    which = which.lower()
    if which == "last":
        return _previous_weekday(index, today)
    if which == "next":
        return _next_week_weekday(index, today)
    monday = today - timedelta(days=today.weekday())
    return monday + timedelta(days=index)


def _next_week_weekday(index: int, today: date) -> date:
    """Friday of the week after this one. Thursday 22 Oct → Friday 30 Oct."""
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    next_monday = today + timedelta(days=days_until_monday)
    return next_monday + timedelta(days=index)


def _previous_weekday(index: int, today: date) -> date:
    delta = (today.weekday() - index) % 7
    if delta == 0:
        delta = 7
    return today - timedelta(days=delta)


def _upcoming_weekday(index: int, today: date) -> date:
    delta = (index - today.weekday()) % 7
    return today + timedelta(days=delta)


def _weekday_index(name: str) -> int:
    return _WEEKDAYS[name.lower()]


def _month_number(name: str) -> int:
    return _MONTHS[name.lower().rstrip(".")]


def _year(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    return int(value)


def _overlaps(span, taken) -> bool:
    start, end = span
    return any(start < other_end and other_start < end for other_start, other_end in taken)


def _connector_between(text: str, left_end: int, right_start: int) -> bool:
    return _CONNECTOR.search(text[left_end:right_start]) is not None
