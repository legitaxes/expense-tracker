import asyncio
import pytest
from app import bot, db, portfolio, tiger


@pytest.fixture
def conn(tmp_path, monkeypatch):
    dbfile = str(tmp_path / "t.db")
    monkeypatch.setattr(bot, "DB_PATH", dbfile)
    c = db.connect(dbfile)
    db.init_schema(c)
    yield c
    c.close()


def test_poll_is_a_noop_when_unconfigured(conn, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not touch the broker when unconfigured")

    monkeypatch.setattr(bot.tiger, "configured", lambda: False)
    monkeypatch.setattr(bot.portfolio, "refresh", boom)
    asyncio.run(bot.poll_portfolio())


def test_poll_swallows_broker_errors(conn, monkeypatch):
    monkeypatch.setattr(bot.tiger, "configured", lambda: True)

    def boom(c, client=None):
        raise RuntimeError("broker down")

    monkeypatch.setattr(bot.portfolio, "refresh", boom)
    # A failed poll must never crash the scheduler — the next tick retries.
    asyncio.run(bot.poll_portfolio())


def test_poll_stores_on_success(conn, monkeypatch):
    monkeypatch.setattr(bot.tiger, "configured", lambda: True)
    pos = tiger.Position(symbol="AAPL", name="AAPL", currency="USD",
                         quantity=1.0, avg_cost_cents=100,
                         market_price_cents=120, market_value_cents=120,
                         unrealized_pl_cents=20, account="U1")
    monkeypatch.setattr(bot.portfolio.tiger, "fetch_portfolio",
                        lambda c=None: tiger.Portfolio(positions=[pos]))
    monkeypatch.setattr(bot.portfolio.fx, "sgd_rate", lambda c, cur: 1.34)

    asyncio.run(bot.poll_portfolio())

    check = db.connect(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    assert len(db.list_positions(check)) == 1
    check.close()
