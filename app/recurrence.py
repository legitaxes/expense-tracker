from __future__ import annotations
from datetime import date, timedelta
import calendar

# index == datetime.date.weekday(): Monday=0 .. Sunday=6
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def weekday_index(name: str) -> int | None:
    try:
        return WEEKDAYS.index(name.strip().lower())
    except ValueError:
        return None


def weekday_name(index: int) -> str:
    return WEEKDAYS[index]


def clamp_day(year: int, month: int, day: int) -> int:
    """Clamp a day-of-month to that month's last valid day (Feb/short months)."""
    return min(day, calendar.monthrange(year, month)[1])


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def first_due(frequency: str, anchor: int | None, today: date) -> date:
    """Earliest date >= today that matches the rule (fires today if today matches)."""
    if frequency == "daily":
        return today
    if frequency == "weekly":
        return today + timedelta(days=(anchor - today.weekday()) % 7)
    if frequency == "monthly":
        candidate = date(today.year, today.month, clamp_day(today.year, today.month, anchor))
        if candidate >= today:
            return candidate
        ny, nm = _next_month(today.year, today.month)
        return date(ny, nm, clamp_day(ny, nm, anchor))
    raise ValueError(f"unknown frequency: {frequency}")


def advance(frequency: str, anchor: int | None, due: date) -> date:
    """The next occurrence strictly after `due`."""
    if frequency == "daily":
        return due + timedelta(days=1)
    if frequency == "weekly":
        return due + timedelta(days=7)
    if frequency == "monthly":
        ny, nm = _next_month(due.year, due.month)
        return date(ny, nm, clamp_day(ny, nm, anchor))
    raise ValueError(f"unknown frequency: {frequency}")


def occurrences_through(frequency: str, anchor: int | None,
                        next_due: date, today: date) -> tuple[list[date], date]:
    """All due dates from next_due through today (inclusive) + the new next_due cursor.

    Back-fill is idempotent: the cursor only moves forward, so re-running with the
    advanced cursor yields no duplicates.
    """
    dates: list[date] = []
    cur = next_due
    while cur <= today:
        dates.append(cur)
        cur = advance(frequency, anchor, cur)
    return dates, cur
