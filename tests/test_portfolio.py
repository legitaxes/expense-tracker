import pytest
from app import db, portfolio, tiger


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


def _pos(symbol="AAPL", currency="USD", qty=10.0, avg=9000, price=9500,
         value=95000, pl=5000):
    return tiger.Position(symbol=symbol, name=symbol, currency=currency,
                          quantity=qty, avg_cost_cents=avg,
                          market_price_cents=price, market_value_cents=value,
                          unrealized_pl_cents=pl, account="U1")


def test_totals_are_grouped_by_currency(conn):
    p = tiger.Portfolio(positions=[_pos("AAPL", "USD", value=95000),
                                   _pos("D05", "SGD", value=40000)])
    portfolio.store(conn, p, {"USD": 1.34, "SGD": 1.0})
    totals = {t["currency"]: t for t in portfolio.current(conn)["totals"]}
    assert totals["USD"]["value_cents"] == 95000
    assert totals["SGD"]["value_cents"] == 40000


def test_totals_sum_multiple_holdings_in_one_currency(conn):
    p = tiger.Portfolio(positions=[_pos("AAPL", value=95000, pl=5000),
                                   _pos("TSLA", value=5000, pl=-1000)])
    portfolio.store(conn, p, {"USD": 1.34})
    total = portfolio.current(conn)["totals"][0]
    assert total["value_cents"] == 100000
    assert total["pl_cents"] == 4000


def test_cost_is_derived_from_quantity_and_avg_cost(conn):
    # 10 shares at S$90.00 average cost = S$900.00 = 90000 cents.
    p = tiger.Portfolio(positions=[_pos(qty=10.0, avg=9000)])
    portfolio.store(conn, p, {"USD": 1.34})
    assert portfolio.current(conn)["totals"][0]["cost_cents"] == 90000


def test_store_writes_one_snapshot_row_per_currency_per_day(conn):
    p = tiger.Portfolio(positions=[_pos("AAPL", "USD"), _pos("D05", "SGD")])
    portfolio.store(conn, p, {"USD": 1.34, "SGD": 1.0}, today="2026-07-27")
    portfolio.store(conn, p, {"USD": 1.35, "SGD": 1.0}, today="2026-07-27")
    rows = db.snapshot_history(conn, days=3650)
    assert len(rows) == 2
    usd = [r for r in rows if r["currency"] == "USD"][0]
    assert usd["sgd_rate"] == 1.35


def test_store_records_last_poll_and_clears_error(conn):
    db.set_setting(conn, "portfolio_last_error", "previous failure")
    portfolio.store(conn, tiger.Portfolio(positions=[_pos()]), {"USD": 1.34})
    assert db.get_setting(conn, "portfolio_last_poll")
    assert db.get_setting(conn, "portfolio_last_error") == ""


def test_current_reports_no_poll_yet(conn):
    state = portfolio.current(conn)
    assert state["positions"] == []
    assert state["last_poll"] is None
    assert state["stale_minutes"] is None


def test_history_points_carry_the_rate_recorded_at_the_time(conn):
    p = tiger.Portfolio(positions=[_pos(value=100000)])
    portfolio.store(conn, p, {"USD": 1.30}, today="2026-07-25")
    portfolio.store(conn, p, {"USD": 1.40}, today="2026-07-26")
    points = portfolio.history(conn, days=3650)["points"]
    rates = [pt["sgd_rate"] for pt in points]
    assert rates == [1.30, 1.40]


def test_refresh_records_error_and_reraises(conn, monkeypatch):
    def boom(client=None):
        raise RuntimeError("broker down")

    monkeypatch.setattr(portfolio.tiger, "fetch_portfolio", boom)
    with pytest.raises(RuntimeError):
        portfolio.refresh(conn)
    assert "broker down" in db.get_setting(conn, "portfolio_last_error")


def test_refresh_leaves_cached_positions_intact_on_failure(conn, monkeypatch):
    portfolio.store(conn, tiger.Portfolio(positions=[_pos()]), {"USD": 1.34})

    def boom(client=None):
        raise RuntimeError("broker down")

    monkeypatch.setattr(portfolio.tiger, "fetch_portfolio", boom)
    with pytest.raises(RuntimeError):
        portfolio.refresh(conn)
    # Last-known-good must survive a failed poll — this is the whole point of
    # the cache-first design.
    assert len(portfolio.current(conn)["positions"]) == 1


def test_refresh_success_stores_and_returns_current(conn, monkeypatch):
    monkeypatch.setattr(portfolio.tiger, "fetch_portfolio",
                        lambda client=None: tiger.Portfolio(positions=[_pos()]))
    monkeypatch.setattr(portfolio.fx, "sgd_rate", lambda c, cur: 1.34)
    state = portfolio.refresh(conn)
    assert len(state["positions"]) == 1
    assert state["last_error"] == ""
