import pytest
from app import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


def _row(symbol="AAPL", currency="USD", value=100000):
    return {"symbol": symbol, "name": symbol + " Inc", "currency": currency,
            "quantity": 10.5, "avg_cost_cents": 9000,
            "market_price_cents": 9500, "market_value_cents": value,
            "unrealized_pl_cents": 5250, "account": "U123"}


def test_replace_positions_is_wholesale(conn):
    db.replace_positions(conn, [_row("AAPL"), _row("TSLA")])
    assert len(db.list_positions(conn)) == 2

    # A later poll with fewer holdings must not leave stale rows behind.
    db.replace_positions(conn, [_row("AAPL")])
    rows = db.list_positions(conn)
    assert [r["symbol"] for r in rows] == ["AAPL"]


def test_replace_positions_with_empty_list_clears(conn):
    db.replace_positions(conn, [_row("AAPL")])
    db.replace_positions(conn, [])
    assert db.list_positions(conn) == []


def test_positions_round_trip_preserves_fractional_quantity(conn):
    db.replace_positions(conn, [_row()])
    row = db.list_positions(conn)[0]
    assert row["quantity"] == 10.5
    assert row["market_value_cents"] == 100000
    assert row["updated_at"]


def test_snapshot_upsert_is_idempotent_per_day_and_currency(conn):
    db.upsert_snapshot(conn, "2026-07-27", "USD", 100000, 5000, 2000, 1.34)
    db.upsert_snapshot(conn, "2026-07-27", "USD", 110000, 5000, 3000, 1.35)
    rows = db.snapshot_history(conn)
    assert len(rows) == 1
    assert rows[0]["total_value_cents"] == 110000
    assert rows[0]["sgd_rate"] == 1.35


def test_snapshot_separates_currencies_same_day(conn):
    db.upsert_snapshot(conn, "2026-07-27", "USD", 100000, 0, 0, 1.34)
    db.upsert_snapshot(conn, "2026-07-27", "SGD", 50000, 0, 0, 1.0)
    assert len(db.snapshot_history(conn)) == 2


def test_snapshot_history_is_chronological_and_windowed(conn):
    from datetime import date, timedelta
    d0 = (date.today() - timedelta(days=2)).isoformat()
    d1 = (date.today() - timedelta(days=1)).isoformat()
    d2 = date.today().isoformat()

    db.upsert_snapshot(conn, d0, "USD", 1, 0, 0, None)
    db.upsert_snapshot(conn, d2, "USD", 3, 0, 0, None)
    db.upsert_snapshot(conn, d1, "USD", 2, 0, 0, None)
    dates = [r["snapshot_date"] for r in db.snapshot_history(conn)]
    assert dates == [d0, d1, d2]

    recent = db.snapshot_history(conn, days=2)
    assert [r["snapshot_date"] for r in recent] == [d1, d2]


def test_snapshot_allows_null_rate(conn):
    db.upsert_snapshot(conn, "2026-07-27", "USD", 100000, 0, 0, None)
    assert db.snapshot_history(conn)[0]["sgd_rate"] is None


def test_snapshot_window_boundary_uses_the_local_clock(conn):
    """days=N must return exactly N local dates, inclusive of today.

    Regression pin: this query used SQLite's date('now'), which is UTC, while
    snapshot_date is written from the LOCAL (SGT, UTC+8) date. Every day
    between midnight and 8am local the two disagreed and the window ran a day
    long -- which is how it was caught, by the suite failing at 00:30 SGT after
    passing all evening.

    Honest about its own limits: if date('now') came back, this can only fail
    DURING that skew window, because for the other 16 hours UTC and SGT share a
    date. It is a boundary pin, not a timezone simulation.
    """
    from datetime import datetime, timedelta
    today = datetime.now().date()
    for i in range(5):
        db.upsert_snapshot(conn, (today - timedelta(days=i)).isoformat(),
                           "USD", i + 1, 0, 0, None)
    got = [r["snapshot_date"] for r in db.snapshot_history(conn, days=3)]
    assert got == [(today - timedelta(days=i)).isoformat() for i in (2, 1, 0)]
