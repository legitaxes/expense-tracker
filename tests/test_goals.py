# tests/test_goals.py
from datetime import date
import pytest
from app import db, goals


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


def test_months_left_is_inclusive_of_current_month():
    assert goals.months_left(date(2026, 7, 31), date(2026, 7, 1)) == 1
    assert goals.months_left(date(2026, 8, 1), date(2026, 7, 25)) == 2
    assert goals.months_left(date(2027, 3, 1), date(2026, 7, 25)) == 9


def test_months_left_zero_or_negative_when_deadline_passed():
    assert goals.months_left(date(2026, 6, 30), date(2026, 7, 1)) == 0
    assert goals.months_left(date(2026, 5, 1), date(2026, 7, 1)) == -1


def test_pace_done_when_target_reached():
    p = goals.pace(300000, 300000, 20000, date(2027, 3, 1), date(2026, 7, 25))
    assert p["status"] == "done"
    assert p["need_per_month"] == 0
    assert p["shortfall_per_month"] == 0


def test_pace_none_without_deadline():
    p = goals.pace(300000, 50000, None, None, date(2026, 7, 25))
    assert p["status"] == "none"
    assert p["need_per_month"] is None
    assert p["months_left"] is None


def test_pace_ok_when_monthly_covers_need():
    # 200000 remaining over 2 months => 100000/mo needed, committed 100000
    p = goals.pace(300000, 100000, 100000, date(2026, 8, 1), date(2026, 7, 25))
    assert p["months_left"] == 2
    assert p["need_per_month"] == 100000
    assert p["status"] == "ok"
    assert p["shortfall_per_month"] == 0


def test_pace_behind_reports_shortfall():
    p = goals.pace(300000, 100000, 60000, date(2026, 8, 1), date(2026, 7, 25))
    assert p["status"] == "behind"
    assert p["shortfall_per_month"] == 40000


def test_pace_behind_when_no_monthly_committed():
    p = goals.pace(300000, 0, None, date(2026, 8, 1), date(2026, 7, 25))
    assert p["status"] == "behind"
    assert p["shortfall_per_month"] == p["need_per_month"] == 150000


def test_pace_overdue_when_deadline_passed_and_short():
    p = goals.pace(300000, 100000, 20000, date(2026, 6, 1), date(2026, 7, 25))
    assert p["status"] == "overdue"
    assert p["need_per_month"] == 200000


def test_need_per_month_rounds_up_so_target_is_actually_met():
    # 10 cents over 3 months must be 4/mo, not 3 (3*3 == 9 < 10)
    p = goals.pace(10, 0, None, date(2026, 9, 1), date(2026, 7, 1))
    assert p["months_left"] == 3
    assert p["need_per_month"] == 4


def test_need_per_month_rises_when_a_month_is_skipped():
    early = goals.pace(300000, 0, None, date(2026, 10, 1), date(2026, 7, 1))
    later = goals.pace(300000, 0, None, date(2026, 10, 1), date(2026, 8, 1))
    assert later["need_per_month"] > early["need_per_month"]


def test_list_with_pace_decorates_each_goal(conn):
    gid = db.add_goal(conn, "Japan", 300000)
    db.update_goal(conn, gid, target_date="2026-08-01", monthly_cents=100000)
    db.add_contribution(conn, gid, 100000)
    g = goals.list_with_pace(conn, today=date(2026, 7, 25))[0]
    assert g["saved_cents"] == 100000
    assert g["pace"]["status"] == "ok"


def _saved(conn, gid):
    return next(g["saved_cents"] for g in db.list_goals(conn) if g["id"] == gid)


def test_fund_due_adds_one_contribution_and_advances_cursor(conn):
    gid = db.add_goal(conn, "Japan", 300000)
    db.update_goal(conn, gid, monthly_cents=20000, next_due="2026-07-01")
    assert goals.fund_due(conn, date(2026, 7, 25)) == [("Japan", 20000)]
    assert _saved(conn, gid) == 20000
    assert db.get_goal(conn, gid)["next_due"] == "2026-08-01"


def test_fund_due_is_idempotent_when_rerun_same_day(conn):
    gid = db.add_goal(conn, "Japan", 300000)
    db.update_goal(conn, gid, monthly_cents=20000, next_due="2026-07-01")
    goals.fund_due(conn, date(2026, 7, 25))
    assert goals.fund_due(conn, date(2026, 7, 25)) == []
    assert _saved(conn, gid) == 20000


def test_fund_due_backfills_every_missed_month(conn):
    gid = db.add_goal(conn, "Japan", 300000)
    db.update_goal(conn, gid, monthly_cents=20000, next_due="2026-04-01")
    # Apr, May, Jun, Jul = 4 occurrences
    assert goals.fund_due(conn, date(2026, 7, 25)) == [("Japan", 80000)]
    assert _saved(conn, gid) == 80000
    assert db.get_goal(conn, gid)["next_due"] == "2026-08-01"


def test_fund_due_clamps_final_contribution_to_remainder(conn):
    gid = db.add_goal(conn, "Small", 25000)
    db.update_goal(conn, gid, monthly_cents=20000, next_due="2026-06-01")
    # Jun + Jul would be 40000, but only 25000 is left to save
    assert goals.fund_due(conn, date(2026, 7, 25)) == [("Small", 25000)]
    assert _saved(conn, gid) == 25000


def test_fund_due_advances_cursor_even_when_nothing_to_add(conn):
    gid = db.add_goal(conn, "Full", 10000)
    db.update_goal(conn, gid, monthly_cents=20000, next_due="2026-07-01")
    db.add_contribution(conn, gid, 10000)
    assert goals.fund_due(conn, date(2026, 7, 25)) == []
    # cursor must still move, or every sweep rescans this goal forever
    assert db.get_goal(conn, gid)["next_due"] == "2026-08-01"


def test_first_of_next_month_rolls_over_december_to_january():
    assert goals.first_of_next_month(date(2026, 12, 15)) == "2027-01-01"


def test_first_of_next_month_within_year():
    assert goals.first_of_next_month(date(2026, 7, 25)) == "2026-08-01"


def test_fund_due_skips_bought_goals(conn):
    gid = db.add_goal(conn, "Done", 300000)
    db.update_goal(conn, gid, monthly_cents=20000, next_due="2026-07-01")
    db.mark_goal_bought(conn, gid, db.add_transaction(conn, 1, "x", "Other", "goal"))
    assert goals.fund_due(conn, date(2026, 7, 25)) == []
    assert _saved(conn, gid) == 0


def test_fund_due_one_bad_goal_does_not_skip_the_rest(conn):
    # An invalid-but-lexically-due next_due (e.g. a corrupt row — month 13
    # still sorts <= today as a string) breaks date.fromisoformat inside the
    # loop for this one goal — the sweep must still fund every other due
    # goal instead of aborting the whole batch.
    bad = db.add_goal(conn, "Bad", 300000)
    db.update_goal(conn, bad, monthly_cents=20000, next_due="2020-13-01")
    good = db.add_goal(conn, "Good", 300000)
    db.update_goal(conn, good, monthly_cents=20000, next_due="2026-07-01")
    funded = goals.fund_due(conn, date(2026, 7, 25))
    assert funded == [("Good", 20000)]
    assert _saved(conn, good) == 20000
    assert _saved(conn, bad) == 0
    # The bad goal's cursor is untouched — it'll be retried (and keep failing
    # loudly in logs) rather than silently skipped forever.
    assert db.get_goal(conn, bad)["next_due"] == "2020-13-01"
