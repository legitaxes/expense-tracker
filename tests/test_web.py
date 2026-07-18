# tests/test_web.py
import pytest
from fastapi.testclient import TestClient
from app import db, web


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbfile = str(tmp_path / "t.db")
    monkeypatch.setattr(web, "DB_PATH", dbfile)
    conn = db.connect(dbfile)
    db.init_schema(conn)

    def override():
        yield conn

    web.app.dependency_overrides[web.get_conn] = override
    with TestClient(web.app) as c:
        yield c
    web.app.dependency_overrides.clear()
    conn.close()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_list_patch_delete(client):
    created = client.post("/api/transactions", json={"amount": 12.5, "note": "lunch", "category": "Food"}).json()
    tid = created["id"]
    assert created["amount_cents"] == 1250 and created["source"] == "dashboard"

    rows = client.get("/api/transactions").json()
    assert len(rows) == 1

    patched = client.patch(f"/api/transactions/{tid}", json={"amount": 9.0, "category": "Transport"}).json()
    assert patched["amount_cents"] == 900 and patched["category"] == "Transport"

    assert client.delete(f"/api/transactions/{tid}").json() == {"deleted": tid}
    assert client.get("/api/transactions").json() == []


def test_reject_unknown_category(client):
    r = client.post("/api/transactions", json={"amount": 1, "note": "x", "category": "Nope"})
    assert r.status_code == 400


def test_summary_and_budgets(client):
    client.post("/api/transactions", json={"amount": 30, "note": "a", "category": "Transport"})
    client.put("/api/budgets/Transport", json={"amount": 100})
    budgets = client.get("/api/budgets").json()
    assert budgets[0] == {"category": "Transport", "limit_cents": 10000, "spent_cents": 3000}
    summary = client.get("/api/summary").json()
    assert summary["total_cents"] == 3000


def test_trend_validation(client):
    assert client.get("/api/trend?granularity=week").status_code == 400


def test_merchants(client):
    client.post("/api/transactions", json={"amount": 5, "note": "Steam", "category": "Entertainment"})
    client.post("/api/transactions", json={"amount": 3, "note": "steam", "category": "Entertainment"})
    from app import db as dbm
    r = client.get("/api/merchants?month=" + dbm.current_year_month()).json()
    assert r["merchants"][0] == {"merchant": "steam", "total_cents": 800, "count": 2,
                                 "category": "Entertainment"}


def test_income_roundtrip_and_cashflow(client):
    created = client.post("/api/transactions",
                          json={"amount": 5000, "note": "salary", "type": "income",
                                "category": "Income"}).json()
    assert created["type"] == "income" and created["category"] == "Income"
    client.post("/api/transactions", json={"amount": 10, "note": "x", "category": "Food"})

    # income invisible to spend endpoints
    from app import db as dbm
    ym = dbm.current_year_month()
    assert client.get("/api/summary?month=" + ym).json()["total_cents"] == 1000

    points = client.get("/api/cashflow").json()["points"]
    assert points[-1]["income_cents"] == 500000 and points[-1]["expense_cents"] == 1000

    # category stays valid on PATCH for income rows
    p = client.patch(f"/api/transactions/{created['id']}", json={"amount": 5100})
    assert p.json()["amount_cents"] == 510000


def test_income_forces_income_category(client):
    r = client.post("/api/transactions",
                    json={"amount": 1, "note": "x", "type": "income", "category": "Food"}).json()
    assert r["category"] == "Income"


def test_patch_cannot_decouple_type_and_category(client):
    exp = client.post("/api/transactions", json={"amount": 1, "note": "x", "category": "Food"}).json()
    inc = client.post("/api/transactions",
                      json={"amount": 1, "note": "y", "type": "income", "category": "Income"}).json()
    assert client.patch(f"/api/transactions/{exp['id']}", json={"category": "Income"}).status_code == 400
    assert client.patch(f"/api/transactions/{inc['id']}", json={"category": "Food"}).status_code == 400
    # valid recategorization still works
    assert client.patch(f"/api/transactions/{exp['id']}", json={"category": "Transport"}).json()["category"] == "Transport"


def test_goals_flow(client):
    g = client.post("/api/goals", json={"name": "Japan", "target": 3000}).json()
    assert g["target_cents"] == 300000 and g["saved_cents"] == 0
    g2 = client.post(f"/api/goals/{g['id']}/contribute", json={"amount": 250.5}).json()
    assert g2["saved_cents"] == 25050
    assert client.get("/api/goals").json()["goals"][0]["saved_cents"] == 25050
    assert client.delete(f"/api/goals/{g['id']}").json() == {"deleted": g["id"]}
    assert client.post("/api/goals/999/contribute", json={"amount": 1}).status_code == 404


def test_budgets_month_param(client):
    client.put("/api/budgets/Food", json={"amount": 100})
    client.post("/api/transactions",
                json={"amount": 20, "note": "old", "category": "Food",
                      "created_at": "2026-06-05T12:00:00"})
    june = client.get("/api/budgets?month=2026-06").json()
    assert june == [{"category": "Food", "limit_cents": 10000, "spent_cents": 2000}]
    other = client.get("/api/budgets?month=2026-05").json()
    assert other[0]["spent_cents"] == 0


def test_pending_flow(client):
    # The client fixture monkeypatches web.DB_PATH to a tmp file — a second
    # connection to the same file is fine (WAL).
    conn = db.connect(web.DB_PATH)
    pid = db.add_pending_import(conn, 1250, "NASI LEMAK STALL", "2026-07-10T12:00:00")
    pid2 = db.add_pending_import(conn, 500000, "salary credited", "2026-07-01T09:00:00",
                                 type_="income")
    conn.close()

    listed = client.get("/api/pending").json()["pending"]
    assert [p["id"] for p in listed] == [pid, pid2]

    tx = client.post(f"/api/pending/{pid}/accept").json()
    assert tx["source"] == "suggest-email" and tx["type"] == "expense"
    assert tx["created_at"] == "2026-07-10T12:00:00"

    tx2 = client.post(f"/api/pending/{pid2}/accept").json()
    assert tx2["category"] == "Income" and tx2["type"] == "income"

    assert client.get("/api/pending").json()["pending"] == []
    assert client.post(f"/api/pending/{pid}/accept").status_code == 404
    assert client.post(f"/api/pending/{pid}/ignore").status_code == 404


def test_pending_ignore(client):
    conn = db.connect(web.DB_PATH)
    pid = db.add_pending_import(conn, 100, "junk", "2026-07-10T12:00:00")
    conn.close()
    assert client.post(f"/api/pending/{pid}/ignore").json() == {"ignored": pid}
    assert client.get("/api/pending").json()["pending"] == []


def test_year_endpoint(client):
    client.post("/api/transactions", json={"amount": 10, "note": "x", "category": "Food"})
    from app import db as dbm
    y = client.get("/api/year/" + dbm.current_year_month()[:4]).json()
    # current year is clamped to the current month — no future bars
    assert y["total_cents"] == 1000
    assert len(y["by_month"]) == int(dbm.current_year_month()[5:7])
    assert client.get("/api/year/26").status_code == 400


def test_csv_export(client):
    client.post("/api/transactions", json={"amount": 2.8, "note": 'kopi, "peng"',
                                           "category": "Food"})
    r = client.get("/api/export.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert "expenses-all.csv" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0] == "id,created_at,type,category,note,amount_sgd,source"
    assert '"kopi, ""peng"""' in lines[1] and "2.80" in lines[1]


def test_delete_budget_endpoint(client):
    client.put("/api/budgets/Food", json={"amount": 300})
    assert client.delete("/api/budgets/Food").json() == {"deleted": "Food"}
    assert client.delete("/api/budgets/Food").status_code == 404


def test_links_endpoint_and_reimbursed_cents(client):
    dinner = client.post("/api/transactions", json={
        "amount": 120, "note": "dinner", "category": "Food"}).json()["id"]
    conn = next(iter(web.app.dependency_overrides[web.get_conn]()))
    db.add_transaction(conn, -4000, "PayNow ALICE", "Food", "suggest-email",
                       link_tid=dinner)
    listed = {r["id"]: r for r in client.get("/api/transactions").json()}
    assert listed[dinner]["reimbursed_cents"] == 4000
    body = client.get(f"/api/transactions/{dinner}/links").json()
    assert body["net_cents"] == 8000 and len(body["links"]) == 1
    assert client.get("/api/transactions/99999/links").status_code == 404


def test_patch_preserves_negative_sign(client):
    conn = next(iter(web.app.dependency_overrides[web.get_conn]()))
    rid = db.add_transaction(conn, -4000, "back", "Food", "suggest-email")
    patched = client.patch(f"/api/transactions/{rid}", json={"amount": 35.0}).json()
    assert patched["amount_cents"] == -3500
