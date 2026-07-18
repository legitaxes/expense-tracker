# tests/test_recurrence.py
from datetime import date, timedelta
import pytest
from app import recurrence


def test_weekday_index_and_name_roundtrip():
    for i in range(7):
        assert recurrence.weekday_index(recurrence.weekday_name(i)) == i
    assert recurrence.weekday_index("MON") == 0
    assert recurrence.weekday_index("sun") == 6
    assert recurrence.weekday_index("funday") is None


@pytest.mark.parametrize("year,month,day,expected", [
    (2026, 2, 31, 28),   # non-leap Feb clamps to 28
    (2024, 2, 31, 29),   # leap Feb clamps to 29
    (2026, 4, 31, 30),   # 30-day month
    (2026, 1, 15, 15),   # no clamp needed
])
def test_clamp_day(year, month, day, expected):
    assert recurrence.clamp_day(year, month, day) == expected


def test_first_due_daily_is_today():
    today = date(2026, 6, 15)
    assert recurrence.first_due("daily", None, today) == today


def test_first_due_weekly_today_when_weekday_matches():
    today = date(2026, 6, 1)
    assert recurrence.first_due("weekly", today.weekday(), today) == today


def test_first_due_weekly_next_match_within_a_week():
    today = date(2026, 6, 1)
    anchor = (today.weekday() + 3) % 7
    result = recurrence.first_due("weekly", anchor, today)
    assert result.weekday() == anchor
    assert today < result <= today + timedelta(days=7)


def test_first_due_monthly_same_month_when_day_not_passed():
    assert recurrence.first_due("monthly", 20, date(2026, 6, 15)) == date(2026, 6, 20)


def test_first_due_monthly_next_month_when_day_passed():
    assert recurrence.first_due("monthly", 1, date(2026, 6, 15)) == date(2026, 7, 1)


def test_first_due_monthly_today_when_day_matches():
    assert recurrence.first_due("monthly", 28, date(2026, 2, 28)) == date(2026, 2, 28)


def test_first_due_monthly_clamps():
    assert recurrence.first_due("monthly", 31, date(2026, 2, 10)) == date(2026, 2, 28)


def test_advance_daily_and_weekly():
    assert recurrence.advance("daily", None, date(2026, 6, 1)) == date(2026, 6, 2)
    assert recurrence.advance("weekly", 0, date(2026, 6, 1)) == date(2026, 6, 8)


def test_advance_monthly_clamps_and_recovers():
    assert recurrence.advance("monthly", 31, date(2026, 1, 31)) == date(2026, 2, 28)
    assert recurrence.advance("monthly", 31, date(2026, 2, 28)) == date(2026, 3, 31)


def test_advance_monthly_year_rollover():
    assert recurrence.advance("monthly", 15, date(2026, 12, 15)) == date(2027, 1, 15)


def test_occurrences_through_daily_backfill():
    dates, nxt = recurrence.occurrences_through("daily", None, date(2026, 6, 1), date(2026, 6, 3))
    assert dates == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    assert nxt == date(2026, 6, 4)


def test_occurrences_through_nothing_due():
    dates, nxt = recurrence.occurrences_through("daily", None, date(2026, 6, 4), date(2026, 6, 3))
    assert dates == []
    assert nxt == date(2026, 6, 4)


def test_occurrences_through_monthly_backfill():
    dates, nxt = recurrence.occurrences_through("monthly", 1, date(2026, 1, 1), date(2026, 3, 10))
    assert dates == [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]
    assert nxt == date(2026, 4, 1)


def test_occurrences_through_monthly_clamps_across_span():
    dates, nxt = recurrence.occurrences_through("monthly", 31, date(2026, 1, 31), date(2026, 3, 31))
    assert dates == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    assert nxt == date(2026, 4, 30)


def test_occurrences_through_weekly_backfill():
    start = date(2026, 6, 1)
    dates, nxt = recurrence.occurrences_through(
        "weekly", start.weekday(), start, start + timedelta(days=14))
    assert dates == [start, start + timedelta(days=7), start + timedelta(days=14)]
    assert nxt == start + timedelta(days=21)


def test_occurrences_through_is_idempotent_when_rerun_with_cursor():
    # The module's headline promise: re-running with the returned cursor yields nothing.
    today = date(2026, 3, 10)
    _dates, nxt = recurrence.occurrences_through("monthly", 1, date(2026, 1, 1), today)
    again, nxt2 = recurrence.occurrences_through("monthly", 1, nxt, today)
    assert again == []
    assert nxt2 == nxt


def test_first_due_weekly_six_days_ahead_wraps_correctly():
    # Worst case for the (anchor - today.weekday()) % 7 modulo: target is 6 days out.
    today = date(2026, 6, 1)
    anchor = (today.weekday() + 6) % 7
    result = recurrence.first_due("weekly", anchor, today)
    assert result == today + timedelta(days=6)
    assert result.weekday() == anchor
