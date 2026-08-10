# tests/test_web.py
import pytest
from fastapi.testclient import TestClient
from app import auth, db, web


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


def _conn():
    """The connection the client fixture injected, so a test can seed state
    the API will read. The override is a generator yielding one connection."""
    return next(web.app.dependency_overrides[web.get_conn]())


def _pending(note, amount_cents=990):
    # NB: add_pending_import takes created_at positionally and has no source arg.
    return db.add_pending_import(_conn(), amount_cents, note, "2026-07-20T10:00:00")


def test_web_accept_honours_trip_mode(client):
    db.set_setting(_conn(), "trip_mode", "1")
    pid = _pending("SHOPEE SG")
    accepted = client.post(f"/api/pending/{pid}/accept").json()
    assert accepted["category"] == "Travel"


def test_web_accept_honours_merchant_memory(client):
    db.remember_merchant(_conn(), "shopee", "Bills")
    pid = _pending("SHOPEE SG")
    accepted = client.post(f"/api/pending/{pid}/accept").json()
    assert accepted["category"] == "Bills"


def test_patch_category_teaches_email_rows(client):
    # trust-email is a learnable source (per the allowlist in app.categories).
    tid = db.add_transaction(_conn(), 1990, "SHOPEE SG", "Shopping", "trust-email",
                             created_at="2026-07-20T10:00:00")
    client.patch(f"/api/transactions/{tid}", json={"category": "Bills"})
    assert db.recall_merchant(_conn(), "shopee") == "Bills"


def test_patch_note_only_does_not_teach(client):
    tid = db.add_transaction(_conn(), 1990, "SHOPEE SG", "Shopping", "trust-email",
                             created_at="2026-07-20T10:00:00")
    client.patch(f"/api/transactions/{tid}", json={"note": "shopee order"})
    assert db.list_merchants(_conn()) == []


def test_patch_category_on_manual_row_does_not_teach(client):
    tid = db.add_transaction(_conn(), 500, "kopi", "Other", "telegram",
                             created_at="2026-07-20T10:00:00")
    client.patch(f"/api/transactions/{tid}", json={"category": "Food"})
    assert db.list_merchants(_conn()) == []


def test_patch_real_edit_sheet_payload_unchanged_category_does_not_teach(client):
    # FINDING 1 regression: static/app.js's saveEdit() always sends `category`
    # (even on an amount/note-only edit), so `patch.category is not None` is
    # true on every save. Against the old code (which only checked that),
    # this amount-edit would re-teach "shopee" -> "Shopping" and wipe out the
    # user's earlier correction of "shopee" -> "Bills". This must fail
    # against the pre-fix behaviour.
    db.remember_merchant(_conn(), "shopee", "Bills")
    tid = db.add_transaction(_conn(), 1990, "SHOPEE SG", "Shopping", "trust-email",
                             created_at="2026-07-20T10:00:00")
    # Real edit-sheet payload: amount + note + category, category unchanged
    # from the row's current category ("Shopping").
    client.patch(f"/api/transactions/{tid}", json={
        "amount": 25.0, "note": "SHOPEE SG", "category": "Shopping"})
    assert db.recall_merchant(_conn(), "shopee") == "Bills"


def test_patch_category_actually_changed_still_teaches(client):
    tid = db.add_transaction(_conn(), 1990, "SHOPEE SG", "Shopping", "trust-email",
                             created_at="2026-07-20T10:00:00")
    client.patch(f"/api/transactions/{tid}", json={
        "amount": 25.0, "note": "SHOPEE SG", "category": "Bills"})
    assert db.recall_merchant(_conn(), "shopee") == "Bills"


def test_create_goal_with_all_fields(client):
    r = client.post("/api/goals", json={
        "name": "Japan", "target": 3000, "target_date": "2027-03-01",
        "monthly": 200, "category": "Travel", "priority": 1, "note": "10 days"})
    assert r.status_code == 200
    g = r.json()
    assert g["target_cents"] == 300000
    assert g["monthly_cents"] == 20000
    assert g["category"] == "Travel"
    assert g["priority"] == 1
    assert g["pace"]["status"] in ("ok", "behind")


def test_create_goal_sets_funding_cursor_when_monthly_given(client):
    g = client.post("/api/goals", json={"name": "J", "target": 100, "monthly": 10}).json()
    assert g["next_due"] is not None


def test_create_goal_leaves_cursor_null_without_monthly(client):
    g = client.post("/api/goals", json={"name": "J", "target": 100}).json()
    assert g["next_due"] is None


def test_create_goal_rejects_bad_category(client):
    r = client.post("/api/goals", json={"name": "J", "target": 100, "category": "Nope"})
    assert r.status_code == 400


def test_create_goal_rejects_bad_priority(client):
    r = client.post("/api/goals", json={"name": "J", "target": 100, "priority": 9})
    assert r.status_code == 400


def test_create_goal_rejects_bad_date(client):
    r = client.post("/api/goals", json={"name": "J", "target": 100, "target_date": "March"})
    assert r.status_code == 400


def test_patch_goal_updates_only_sent_fields(client):
    gid = client.post("/api/goals", json={"name": "J", "target": 100,
                                          "note": "keep"}).json()["id"]
    g = client.patch(f"/api/goals/{gid}", json={"priority": 3}).json()
    assert g["priority"] == 3
    assert g["note"] == "keep"


def test_patch_goal_empty_string_clears_the_deadline(client):
    gid = client.post("/api/goals", json={"name": "J", "target": 100,
                                          "target_date": "2027-01-01"}).json()["id"]
    g = client.patch(f"/api/goals/{gid}", json={"target_date": ""}).json()
    assert g["target_date"] is None
    assert g["pace"]["status"] == "none"


def test_patch_goal_setting_monthly_starts_the_cursor(client):
    gid = client.post("/api/goals", json={"name": "J", "target": 100}).json()["id"]
    g = client.patch(f"/api/goals/{gid}", json={"monthly": 10}).json()
    assert g["next_due"] is not None


def test_patch_goal_clearing_monthly_clears_the_cursor(client):
    gid = client.post("/api/goals", json={"name": "J", "target": 100,
                                          "monthly": 10}).json()["id"]
    g = client.patch(f"/api/goals/{gid}", json={"monthly": ""}).json()
    assert g["monthly_cents"] is None
    assert g["next_due"] is None


def test_patch_goal_rejects_non_numeric_monthly_string(client):
    gid = client.post("/api/goals", json={"name": "J", "target": 100}).json()["id"]
    r = client.patch(f"/api/goals/{gid}", json={"monthly": "abc"})
    assert r.status_code == 400


def test_patch_missing_goal_is_404(client):
    assert client.patch("/api/goals/999", json={"priority": 1}).status_code == 404


def test_create_goal_rejects_negative_target(client):
    r = client.post("/api/goals", json={"name": "J", "target": -100})
    assert r.status_code == 400


def test_create_goal_rejects_negative_monthly(client):
    r = client.post("/api/goals", json={"name": "J", "target": 100, "monthly": -5})
    assert r.status_code == 400


def test_patch_goal_rejects_negative_target(client):
    gid = client.post("/api/goals", json={"name": "J", "target": 100}).json()["id"]
    r = client.patch(f"/api/goals/{gid}", json={"target": -100})
    assert r.status_code == 400


def test_patch_goal_rejects_negative_monthly_number(client):
    gid = client.post("/api/goals", json={"name": "J", "target": 100}).json()["id"]
    r = client.patch(f"/api/goals/{gid}", json={"monthly": -5})
    assert r.status_code == 400


def test_patch_goal_rejects_negative_monthly_string(client):
    gid = client.post("/api/goals", json={"name": "J", "target": 100}).json()["id"]
    r = client.patch(f"/api/goals/{gid}", json={"monthly": "-5"})
    assert r.status_code == 400


def test_patch_goal_zero_monthly_still_clears(client):
    gid = client.post("/api/goals", json={"name": "J", "target": 100,
                                          "monthly": 10}).json()["id"]
    g = client.patch(f"/api/goals/{gid}", json={"monthly": 0}).json()
    assert g["monthly_cents"] is None
    assert g["next_due"] is None


def test_create_goal_normalizes_target_date(client):
    g = client.post("/api/goals", json={"name": "J", "target": 100,
                                        "target_date": "20270301"}).json()
    assert g["target_date"] == "2027-03-01"


def test_patch_goal_normalizes_target_date(client):
    # Week-date form: Monday of ISO week 9, 2027 == 2027-03-01. Picked over a
    # datetime-with-time string because date.fromisoformat's acceptance of
    # the latter is Python-3.12-specific and this suite may run on 3.11.
    gid = client.post("/api/goals", json={"name": "J", "target": 100}).json()["id"]
    g = client.patch(f"/api/goals/{gid}", json={"target_date": "2027-W09-1"}).json()
    assert g["target_date"] == "2027-03-01"


def test_bought_creates_expense_at_target_by_default(client):
    gid = client.post("/api/goals", json={"name": "Laptop", "target": 2000,
                                          "category": "Shopping"}).json()["id"]
    g = client.post(f"/api/goals/{gid}/bought", json={}).json()
    assert g["bought_at"] is not None
    # NOTE: test_web.py's client fixture exposes GET /api/transactions as a
    # bare list (see test_create_list_patch_delete above), not
    # {"transactions": [...]} as the task brief assumed. Matching the real
    # file's shape here.
    tx = [t for t in client.get("/api/transactions").json()
          if t["id"] == g["bought_tid"]]
    assert len(tx) == 1
    assert tx[0]["amount_cents"] == 200000
    assert tx[0]["category"] == "Shopping"


def test_bought_uses_actual_price_when_given(client):
    gid = client.post("/api/goals", json={"name": "Laptop", "target": 2000}).json()["id"]
    g = client.post(f"/api/goals/{gid}/bought", json={"price": 1899}).json()
    tx = [t for t in client.get("/api/transactions").json()
          if t["id"] == g["bought_tid"]][0]
    assert tx["amount_cents"] == 189900
    assert tx["category"] == "Other"   # no goal category -> DEFAULT_CATEGORY


def test_bought_twice_is_400(client):
    gid = client.post("/api/goals", json={"name": "L", "target": 10}).json()["id"]
    client.post(f"/api/goals/{gid}/bought", json={})
    assert client.post(f"/api/goals/{gid}/bought", json={}).status_code == 400


# ---- web-side reimbursement linking ---------------------------------------

def _mk_income(client, cents, note="PayNow from Wei"):
    r = client.post("/api/transactions",
                    json={"amount": cents / 100, "note": note,
                          "category": "Income", "type": "income"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_expense(client, cents, note, cat="Food"):
    r = client.post("/api/transactions",
                    json={"amount": cents / 100, "note": note, "category": cat})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_candidates_are_offered_for_an_incoming_transfer(client):
    dinner = _mk_expense(client, 6000, "Dinner")
    _mk_expense(client, 280, "Kopi")           # below the transfer amount
    tid = _mk_income(client, 3000)
    got = client.get(f"/api/transactions/{tid}/reimburse-candidates").json()
    ids = [c["id"] for c in got["candidates"]]
    assert dinner in ids
    # The floor is the point: a S$2.80 kopi cannot be paying back S$30.
    assert all(c["amount_cents"] >= 3000 for c in got["candidates"])


def test_candidates_rejected_for_a_spend(client):
    kopi = _mk_expense(client, 280, "Kopi")
    r = client.get(f"/api/transactions/{kopi}/reimburse-candidates")
    assert r.status_code == 400


def test_link_then_unlink_round_trip_over_http(client):
    dinner = _mk_expense(client, 6000, "Dinner")
    tid = _mk_income(client, 3000)
    linked = client.post(f"/api/transactions/{tid}/link",
                         json={"target_tid": dinner})
    assert linked.status_code == 200
    body = linked.json()
    assert body["amount_cents"] == -3000
    assert body["category"] == "Food"
    assert body["link_tid"] == dinner
    # Reflected on the original, which is what the user sees on the row.
    detail = client.get(f"/api/transactions/{dinner}/links").json()
    assert detail["net_cents"] == 3000
    assert [l["id"] for l in detail["links"]] == [tid]

    back = client.post(f"/api/transactions/{tid}/unlink")
    assert back.status_code == 200
    assert back.json()["amount_cents"] == 3000
    assert back.json()["type"] == "income"
    assert client.get(f"/api/transactions/{dinner}/links").json()["net_cents"] == 6000


def test_link_refuses_an_impossible_pair(client):
    dinner = _mk_expense(client, 6000, "Dinner")
    kopi = _mk_expense(client, 280, "Kopi")
    assert client.post(f"/api/transactions/{kopi}/link",
                       json={"target_tid": dinner}).status_code == 400
    tid = _mk_income(client, 3000)
    assert client.post(f"/api/transactions/{tid}/link",
                       json={"target_tid": 999999}).status_code == 400


def test_unlink_refuses_an_unlinked_row(client):
    kopi = _mk_expense(client, 280, "Kopi")
    assert client.post(f"/api/transactions/{kopi}/unlink").status_code == 400


def test_summary_accepts_a_card_filter(client):
    cid = client.post("/api/cards",
                      json={"name": "Trust", "kind": "debit"}).json()["id"]
    a = client.post("/api/transactions",
                    json={"amount": 1.0, "note": "a", "category": "Food"}).json()
    client.post("/api/transactions",
                json={"amount": 2.0, "note": "b", "category": "Food"})
    client.patch("/api/transactions/%d" % a["id"], json={"card_id": cid})
    assert client.get("/api/summary").json()["total_cents"] == 300
    assert client.get("/api/summary?card_id=%d" % cid).json()["total_cents"] == 100
    # 0 must reach db as the Unassigned bucket, not be dropped as falsy.
    assert client.get("/api/summary?card_id=0").json()["total_cents"] == 200


def test_trend_accepts_a_card_filter(client):
    cid = client.post("/api/cards",
                      json={"name": "Trust", "kind": "debit"}).json()["id"]
    a = client.post("/api/transactions",
                    json={"amount": 1.0, "note": "a", "category": "Food"}).json()
    client.post("/api/transactions",
                json={"amount": 2.0, "note": "b", "category": "Food"})
    client.patch("/api/transactions/%d" % a["id"], json={"card_id": cid})
    pts = client.get("/api/trend?granularity=day&card_id=%d" % cid).json()["points"]
    assert sum(p["total_cents"] for p in pts) == 100
    pts = client.get("/api/trend?granularity=day&card_id=0").json()["points"]
    assert sum(p["total_cents"] for p in pts) == 200


def test_card_filter_rejects_a_negative_card_id(client):
    # db._card_clause raises ValueError on a negative id by design; without a
    # boundary constraint that would be a 500 instead of a client error.
    assert client.get("/api/summary?card_id=-1").status_code == 422
    assert client.get("/api/trend?card_id=-1").status_code == 422
