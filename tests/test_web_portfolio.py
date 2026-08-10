import pytest
from fastapi.testclient import TestClient
from app import auth, db, web, tiger, portfolio


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbfile = str(tmp_path / "t.db")
    monkeypatch.setattr(web, "DB_PATH", dbfile)
    monkeypatch.setattr(web, "SIGNUP_CODE", "")
    auth.reset_throttle()
    conn = db.connect(dbfile)
    db.init_schema(conn)

    def override():
        yield conn

    web.app.dependency_overrides[web.get_conn] = override
    with TestClient(web.app) as c:
        # Every route is gated now; these tests all assume a signed-in user.
        c.post("/api/auth/signup",
               json={"username": "tester", "password": "hunter2hunter2"})
        c.conn = conn
        yield c
    web.app.dependency_overrides.clear()
    conn.close()


def _pos(symbol="AAPL", currency="USD", value=95000):
    return tiger.Position(symbol=symbol, name=symbol, currency=currency,
                          quantity=10.0, avg_cost_cents=9000,
                          market_price_cents=9500, market_value_cents=value,
                          unrealized_pl_cents=5000, account="U1")


def test_portfolio_reports_unconfigured(client, monkeypatch):
    monkeypatch.setattr(web.tiger, "configured", lambda: False)
    body = client.get("/api/portfolio").json()
    assert body == {"configured": False}


def test_portfolio_returns_cached_state(client, monkeypatch):
    monkeypatch.setattr(web.tiger, "configured", lambda: True)
    portfolio.store(client.conn, tiger.Portfolio(positions=[_pos()]), {"USD": 1.34})
    body = client.get("/api/portfolio").json()
    assert body["configured"] is True
    assert len(body["positions"]) == 1
    assert body["totals"][0]["value_cents"] == 95000


def test_history_route(client, monkeypatch):
    monkeypatch.setattr(web.tiger, "configured", lambda: True)
    portfolio.store(client.conn, tiger.Portfolio(positions=[_pos()]),
                    {"USD": 1.34}, today="2026-07-27")
    points = client.get("/api/portfolio/history?days=3650").json()["points"]
    assert points[0]["date"] == "2026-07-27"
    assert points[0]["sgd_rate"] == 1.34


def test_history_rejects_bad_days(client, monkeypatch):
    monkeypatch.setattr(web.tiger, "configured", lambda: True)
    assert client.get("/api/portfolio/history?days=0").status_code == 400
    assert client.get("/api/portfolio/history?days=99999").status_code == 400


def test_refresh_success(client, monkeypatch):
    monkeypatch.setattr(web.tiger, "configured", lambda: True)
    monkeypatch.setattr(web.portfolio.tiger, "fetch_portfolio",
                        lambda c=None: tiger.Portfolio(positions=[_pos()]))
    monkeypatch.setattr(web.portfolio.fx, "sgd_rate", lambda c, cur: 1.34)
    body = client.post("/api/portfolio/refresh").json()
    assert len(body["positions"]) == 1


def test_refresh_returns_503_on_broker_failure(client, monkeypatch):
    monkeypatch.setattr(web.tiger, "configured", lambda: True)

    def boom(c=None):
        raise RuntimeError("broker down")

    monkeypatch.setattr(web.portfolio.tiger, "fetch_portfolio", boom)
    resp = client.post("/api/portfolio/refresh")
    assert resp.status_code == 503
    assert "broker down" in resp.json()["detail"]


def test_refresh_503_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(web.tiger, "configured", lambda: False)
    assert client.post("/api/portfolio/refresh").status_code == 503
