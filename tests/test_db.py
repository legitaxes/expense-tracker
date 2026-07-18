# tests/test_db.py
import pytest
from app import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


def test_add_and_get(conn):
    tid = db.add_transaction(conn, 1250, "lunch", "Food", "telegram",
                             created_at="2026-05-31T12:00:00")
    row = db.get_transaction(conn, tid)
    assert row["amount_cents"] == 1250
    assert row["note"] == "lunch"
    assert row["category"] == "Food"
    assert row["source"] == "telegram"


def test_list_filters_by_category_and_date(conn):
    db.add_transaction(conn, 100, "a", "Transport", "telegram", "2026-05-01T08:00:00")
    db.add_transaction(conn, 200, "b", "Food", "telegram", "2026-05-15T08:00:00")
    db.add_transaction(conn, 300, "c", "Transport", "telegram", "2026-06-02T08:00:00")
    transport_may = db.list_transactions(conn, date_from="2026-05-01",
                                         date_to="2026-05-31", category="Transport")
    assert [r["amount_cents"] for r in transport_may] == [100]


def test_update_and_delete(conn):
    tid = db.add_transaction(conn, 100, "typo", "Other", "telegram", "2026-05-31T08:00:00")
    assert db.update_transaction(conn, tid, amount_cents=250, category="Transport") is True
    row = db.get_transaction(conn, tid)
    assert row["amount_cents"] == 250 and row["category"] == "Transport"
    assert db.delete_transaction(conn, tid) is True
    assert db.get_transaction(conn, tid) is None


def test_latest_transaction_id(conn):
    assert db.latest_transaction_id(conn) is None
    db.add_transaction(conn, 100, "a", "Other", "telegram", "2026-05-31T08:00:00")
    last = db.add_transaction(conn, 200, "b", "Other", "telegram", "2026-05-31T09:00:00")
    assert db.latest_transaction_id(conn) == last


def test_month_summary(conn):
    db.add_transaction(conn, 1000, "a", "Transport", "telegram", "2026-05-02T08:00:00")
    db.add_transaction(conn, 2000, "b", "Food", "telegram", "2026-05-10T08:00:00")
    db.add_transaction(conn, 500, "c", "Transport", "telegram", "2026-05-20T08:00:00")
    db.add_transaction(conn, 999, "d", "Transport", "telegram", "2026-06-01T08:00:00")
    s = db.month_summary(conn, "2026-05")
    assert s["total_cents"] == 3500
    cats = {r["category"]: r["total_cents"] for r in s["by_category"]}
    assert cats == {"Transport": 1500, "Food": 2000}


def test_trend_by_month(conn):
    db.add_transaction(conn, 1000, "a", "Transport", "telegram", "2026-04-02T08:00:00")
    db.add_transaction(conn, 2000, "b", "Transport", "telegram", "2026-05-02T08:00:00")
    db.add_transaction(conn, 500, "c", "Transport", "telegram", "2026-05-09T08:00:00")
    points = db.trend(conn, "month")
    assert points == [
        {"period": "2026-04", "total_cents": 1000, "by_category": {"Transport": 1000}},
        {"period": "2026-05", "total_cents": 2500, "by_category": {"Transport": 2500}},
    ]


def test_trend_by_day(conn):
    db.add_transaction(conn, 100, "a", "Transport", "telegram", "2026-05-01T08:00:00")
    db.add_transaction(conn, 200, "b", "Transport", "telegram", "2026-05-01T20:00:00")
    db.add_transaction(conn, 50, "c", "Food", "telegram", "2026-05-01T12:00:00")
    points = db.trend(conn, "day")
    assert points == [{"period": "2026-05-01", "total_cents": 350,
                       "by_category": {"Transport": 300, "Food": 50}}]


def test_set_and_get_budgets(conn):
    db.set_budget(conn, "Transport", 30000)
    db.set_budget(conn, "Transport", 25000)   # upsert overwrites
    db.set_budget(conn, "Food", 40000)
    assert db.get_budgets(conn) == {"Transport": 25000, "Food": 40000}


def test_month_category_spend(conn):
    db.add_transaction(conn, 1000, "a", "Transport", "telegram", "2026-05-02T08:00:00")
    db.add_transaction(conn, 500, "b", "Transport", "telegram", "2026-05-20T08:00:00")
    db.add_transaction(conn, 999, "c", "Transport", "telegram", "2026-06-01T08:00:00")
    assert db.month_category_spend(conn, "2026-05", "Transport") == 1500
    assert db.month_category_spend(conn, "2026-05", "Food") == 0


def test_alerts_fire_once_per_threshold(conn):
    db.set_budget(conn, "Transport", 10000)   # S$100 budget
    # Spend S$82 -> crosses 80 only
    db.add_transaction(conn, 8200, "a", "Transport", "telegram", "2026-05-02T08:00:00")
    crossed = db.check_and_record_alerts(conn, "Transport", "2026-05", (80, 100))
    assert crossed == [80]
    # Same call again -> nothing new (idempotent)
    assert db.check_and_record_alerts(conn, "Transport", "2026-05", (80, 100)) == []
    # Spend more, now over 100 -> only 100 newly crossed
    db.add_transaction(conn, 3000, "b", "Transport", "telegram", "2026-05-03T08:00:00")
    assert db.check_and_record_alerts(conn, "Transport", "2026-05", (80, 100)) == [100]


def test_no_alert_without_budget(conn):
    db.add_transaction(conn, 99999, "a", "Entertainment", "telegram", "2026-05-02T08:00:00")
    assert db.check_and_record_alerts(conn, "Entertainment", "2026-05", (80, 100)) == []


def test_recurring_crud(conn):
    rid = db.add_recurring(conn, 500, "coffee", "Food", "daily", None, "2026-06-01")
    row = db.get_recurring(conn, rid)
    assert row["amount_cents"] == 500
    assert row["note"] == "coffee"
    assert row["category"] == "Food"
    assert row["frequency"] == "daily"
    assert row["anchor"] is None
    assert row["next_due"] == "2026-06-01"
    assert row["active"] == 1


def test_list_recurring_active_only_and_order(conn):
    r_late = db.add_recurring(conn, 100, "b", "Other", "monthly", 28, "2026-06-28")
    r_early = db.add_recurring(conn, 200, "a", "Other", "daily", None, "2026-06-01")
    r_off = db.add_recurring(conn, 300, "c", "Other", "daily", None, "2026-06-01")
    db.deactivate_recurring(conn, r_off)
    assert [r["id"] for r in db.list_recurring(conn)] == [r_early, r_late]
    assert {r["id"] for r in db.list_recurring(conn, active_only=False)} == {r_early, r_late, r_off}


def test_due_recurring_filters(conn):
    due = db.add_recurring(conn, 100, "due", "Other", "daily", None, "2026-06-01")
    db.add_recurring(conn, 100, "future", "Other", "daily", None, "2026-12-31")
    inactive = db.add_recurring(conn, 100, "inactive", "Other", "daily", None, "2026-06-01")
    db.deactivate_recurring(conn, inactive)
    assert [r["id"] for r in db.due_recurring(conn, "2026-06-15")] == [due]


def test_set_next_due(conn):
    rid = db.add_recurring(conn, 100, "x", "Other", "monthly", 1, "2026-06-01")
    db.set_next_due(conn, rid, "2026-07-01")
    assert db.get_recurring(conn, rid)["next_due"] == "2026-07-01"


def test_deactivate_recurring_idempotent(conn):
    rid = db.add_recurring(conn, 100, "x", "Other", "daily", None, "2026-06-01")
    assert db.deactivate_recurring(conn, rid) is True
    assert db.get_recurring(conn, rid)["active"] == 0
    assert db.deactivate_recurring(conn, rid) is False   # already inactive
    assert db.deactivate_recurring(conn, 9999) is False  # missing


def test_merchant_leaderboard(conn):
    db.add_transaction(conn, 500, "Kopitiam", "Food", "telegram", "2026-07-01T08:00:00")
    db.add_transaction(conn, 300, "kopitiam", "Food", "telegram", "2026-07-02T08:00:00")
    db.add_transaction(conn, 900, "Steam", "Entertainment", "telegram", "2026-07-03T08:00:00")
    db.add_transaction(conn, 100, "june thing", "Food", "telegram", "2026-06-01T08:00:00")
    db.add_transaction(conn, 400, "", "Other", "telegram", "2026-07-04T08:00:00")
    rows = db.merchant_leaderboard(conn, "2026-07")
    assert rows[0] == {"merchant": "steam", "total_cents": 900, "count": 1,
                       "category": "Entertainment"}
    assert rows[1] == {"merchant": "kopitiam", "total_cents": 800, "count": 2,
                       "category": "Food"}
    assert len(rows) == 2  # other month + empty note excluded


def test_type_column_migration(tmp_path):
    dbfile = str(tmp_path / "old.db")
    c = db.connect(dbfile)
    c.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "amount_cents INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', "
              "category TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL)")
    c.execute("INSERT INTO transactions (amount_cents, note, category, created_at, source) "
              "VALUES (100,'x','Food','2026-07-01T00:00:00','telegram')")
    c.commit()
    db.init_schema(c)  # must add the column without touching existing rows
    row = c.execute("SELECT * FROM transactions").fetchone()
    assert row["type"] == "expense" and row["amount_cents"] == 100
    c.close()


def test_income_excluded_from_spend_aggregates(conn):
    db.add_transaction(conn, 1000, "lunch", "Food", "telegram", "2026-07-01T12:00:00")
    db.add_transaction(conn, 500000, "salary", "Income", "telegram",
                       "2026-07-01T09:00:00", type_="income")
    s = db.month_summary(conn, "2026-07")
    assert s["total_cents"] == 1000
    assert [c["category"] for c in s["by_category"]] == ["Food"]
    assert db.month_category_spend(conn, "2026-07", "Income") == 0
    assert db.trend(conn, "month") == [{"period": "2026-07", "total_cents": 1000,
                                        "by_category": {"Food": 1000}}]
    assert db.merchant_leaderboard(conn, "2026-07") == [
        {"merchant": "lunch", "total_cents": 1000, "count": 1, "category": "Food"}]
    assert db.month_income(conn, "2026-07") == 500000


def test_cashflow(conn):
    db.add_transaction(conn, 1000, "a", "Food", "telegram", "2026-06-01T12:00:00")
    db.add_transaction(conn, 300000, "pay", "Income", "telegram",
                       "2026-06-01T09:00:00", type_="income")
    db.add_transaction(conn, 2000, "b", "Food", "telegram", "2026-07-01T12:00:00")
    assert db.cashflow(conn) == [
        {"period": "2026-06", "income_cents": 300000, "expense_cents": 1000},
        {"period": "2026-07", "income_cents": 0, "expense_cents": 2000},
    ]


def test_goals_crud_and_progress(conn):
    gid = db.add_goal(conn, "Japan Trip", 300000)
    db.add_contribution(conn, gid, 20000)
    db.add_contribution(conn, gid, 5000)
    goals = db.list_goals(conn)
    assert goals == [{"id": gid, "name": "Japan Trip", "target_cents": 300000,
                      "saved_cents": 25000}]
    assert db.delete_goal(conn, gid) is True
    assert db.list_goals(conn) == []
    # cascade: contributions gone too
    n = conn.execute("SELECT COUNT(*) AS n FROM goal_contributions").fetchone()["n"]
    assert n == 0


def test_find_goal_matching(conn):
    db.add_goal(conn, "Japan Trip", 300000)
    db.add_goal(conn, "Jacket", 10000)
    assert db.find_goal(conn, "japan trip")["name"] == "Japan Trip"
    assert db.find_goal(conn, "jac")["name"] == "Jacket"
    assert db.find_goal(conn, "ja") is None       # ambiguous prefix
    assert db.find_goal(conn, "nope") is None


def test_find_goal_ignores_like_wildcards(conn):
    db.add_goal(conn, "AB", 1000)
    db.add_goal(conn, "AXB", 1000)
    assert db.find_goal(conn, "a_") is None       # _ is not a wildcard
    assert db.find_goal(conn, "a%") is None       # % is not a wildcard


def test_year_summary(conn, monkeypatch):
    monkeypatch.setattr(db, "current_year_month", lambda: "2026-07")
    db.add_transaction(conn, 1000, "a", "Food", "telegram", "2026-01-05T12:00:00")
    db.add_transaction(conn, 2000, "b", "Food", "telegram", "2026-07-05T12:00:00")
    db.add_transaction(conn, 500, "c", "Transport", "telegram", "2026-07-06T12:00:00")
    db.add_transaction(conn, 99999, "old", "Food", "telegram", "2025-12-31T12:00:00")
    db.add_transaction(conn, 300000, "pay", "Income", "telegram",
                       "2026-07-01T09:00:00", type_="income")
    y = db.year_summary(conn, "2026")
    assert y["year"] == "2026"
    assert y["total_cents"] == 3500          # 2025 row excluded, income excluded
    assert y["income_cents"] == 300000
    assert len(y["by_month"]) == 7           # current year: months after "now" dropped
    assert y["by_month"][0] == {"month": "01", "total_cents": 1000,
                                "by_category": {"Food": 1000}}
    assert y["by_month"][6] == {"month": "07", "total_cents": 2500,
                                "by_category": {"Food": 2000, "Transport": 500}}
    assert y["by_month"][2] == {"month": "03", "total_cents": 0, "by_category": {}}
    assert y["by_category"][0] == {"category": "Food", "total_cents": 3000}


def test_year_summary_past_year_keeps_all_months(conn, monkeypatch):
    monkeypatch.setattr(db, "current_year_month", lambda: "2026-07")
    db.add_transaction(conn, 99999, "old", "Food", "telegram", "2025-12-31T12:00:00")
    y = db.year_summary(conn, "2025")
    assert len(y["by_month"]) == 12
    assert y["by_month"][11]["total_cents"] == 99999


def test_settings_roundtrip(conn):
    assert db.get_setting(conn, "trip_mode") is None
    assert db.get_setting(conn, "trip_mode", "0") == "0"
    db.set_setting(conn, "trip_mode", "1")
    assert db.get_setting(conn, "trip_mode", "0") == "1"
    db.set_setting(conn, "trip_mode", "0")
    assert db.get_setting(conn, "trip_mode") == "0"


def test_delete_budget(conn):
    db.set_budget(conn, "Food", 30000)
    assert db.delete_budget(conn, "Food") is True
    assert db.get_budgets(conn) == {}
    assert db.delete_budget(conn, "Food") is False


def test_frequent_notes(conn):
    for amt in (280, 300, 260):
        db.add_transaction(conn, amt, "Kopi", "Food", "telegram")
    db.add_transaction(conn, 1200, "lunch", "Food", "telegram")
    db.add_transaction(conn, 100, "kopi", "Food", "telegram",
                       "2020-01-01T08:00:00")            # outside 90d window
    db.add_transaction(conn, 500000, "salary", "Income", "telegram", type_="income")
    db.add_transaction(conn, 999, "", "Other", "telegram")  # empty note excluded
    out = db.frequent_notes(conn)
    assert out[0]["note"].lower() == "kopi"
    assert out[0]["count"] == 3
    assert out[0]["median_cents"] == 280
    assert [o["note"] for o in out[1:]] == ["lunch"]


def test_search_transactions_escapes_like(conn):
    db.add_transaction(conn, 100, "50% off sale", "Shopping", "telegram")
    db.add_transaction(conn, 200, "kopi", "Food", "telegram")
    assert [r["note"] for r in db.search_transactions(conn, "50%")] == ["50% off sale"]
    assert db.search_transactions(conn, "_") == []
    assert len(db.search_transactions(conn, "KOPI")) == 1  # case-insensitive


def test_record_pace_alert_dedups(conn):
    assert db.record_pace_alert(conn, "Food", "2026-07") is True
    assert db.record_pace_alert(conn, "Food", "2026-07") is False
    assert db.record_pace_alert(conn, "Food", "2026-08") is True


def test_reimbursement_link_and_netting(conn):
    dinner = db.add_transaction(conn, 12000, "dinner", "Food", "telegram",
                                "2026-07-15T20:00:00")
    db.add_transaction(conn, -4000, "PayNow ALICE", "Food", "suggest-email",
                       "2026-07-16T09:00:00", link_tid=dinner)
    links = db.get_links(conn, dinner)
    assert [l["amount_cents"] for l in links] == [-4000]
    listed = {r["id"]: r for r in db.list_transactions(conn)}
    assert listed[dinner]["reimbursed_cents"] == 4000
    assert listed[links[0]["id"]]["reimbursed_cents"] == 0


def test_delete_original_nulls_links(conn):
    dinner = db.add_transaction(conn, 12000, "dinner", "Food", "telegram",
                                "2026-07-15T20:00:00")
    reimb = db.add_transaction(conn, -4000, "back", "Food", "suggest-email",
                               "2026-07-16T09:00:00", link_tid=dinner)
    db.delete_transaction(conn, dinner)
    assert db.get_transaction(conn, reimb)["link_tid"] is None


def test_recent_expense_candidates_window_and_floor(conn):
    from datetime import datetime, timedelta
    now = datetime.now()
    iso = lambda dt: dt.isoformat(timespec="seconds")
    old = db.add_transaction(conn, 9000, "old", "Food", "telegram",
                             iso(now - timedelta(days=10)))
    small = db.add_transaction(conn, 1000, "small", "Food", "telegram", iso(now))
    match = db.add_transaction(conn, 12000, "dinner", "Food", "telegram",
                               iso(now - timedelta(days=1)))
    ids = [r["id"] for r in db.recent_expense_candidates(conn, 4000)]
    assert match in ids and old not in ids and small not in ids
