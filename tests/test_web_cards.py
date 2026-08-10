"""Cards CRUD and per-card breakdown web routes: gating, validation, shape.

Fixtures follow the pattern in tests/test_web_payslip.py (client + conn) and
tests/test_auth_web.py (anon) -- test_web_payslip.py itself has no
unauthenticated-client fixture, so `anon` here is the auth_web.py one.
"""
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
        c.post("/api/auth/signup",
               json={"username": "tester", "password": "hunter2hunter2"})
        c.conn = conn
        yield c
    web.app.dependency_overrides.clear()
    conn.close()


@pytest.fixture
def conn(client):
    return client.conn


@pytest.fixture
def anon(tmp_path, monkeypatch):
    """A client with no session and an empty user table."""
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
        yield c
    web.app.dependency_overrides.clear()
    conn.close()


def test_cards_routes_require_a_session(anon):
    assert anon.get("/api/cards").status_code == 401
    assert anon.post("/api/cards", json={"name": "X", "kind": "debit"}).status_code == 401
    assert anon.get("/api/cards/breakdown").status_code == 401


def test_create_and_list(client):
    r = client.post("/api/cards", json={"name": "Trust Link", "kind": "debit",
                                        "last4": "9012"})
    assert r.status_code == 200
    cid = r.json()["id"]
    assert [c["name"] for c in client.get("/api/cards").json()["cards"]] == ["Trust Link"]
    assert client.get("/api/cards").json()["cards"][0]["tx_count"] == 0
    assert cid > 0


def test_kind_is_validated(client):
    r = client.post("/api/cards", json={"name": "X", "kind": "cryptocurrency"})
    assert r.status_code == 400


def test_binding_a_source_at_creation_writes_the_default(client, conn):
    from app import cards as cards_mod
    cid = client.post("/api/cards", json={"name": "Trust Link", "kind": "debit",
                                          "bind_source": "trust-email"}).json()["id"]
    assert cards_mod.effective_card(conn, "trust-email", "anything") == (cid, "source")


def test_an_unknown_bind_source_is_rejected(client):
    r = client.post("/api/cards", json={"name": "X", "kind": "debit",
                                        "bind_source": "not-a-sender"})
    assert r.status_code == 400


def test_delete_archives_a_referenced_card(client, conn):
    from app import db as db_mod
    cid = client.post("/api/cards", json={"name": "Trust Link",
                                          "kind": "debit"}).json()["id"]
    db_mod.add_transaction(conn, 500, "kopi", "Food", "trust-email", card_id=cid)
    assert client.delete(f"/api/cards/{cid}").json()["archived"] is True
    assert client.get("/api/cards").json()["cards"] == []
    assert db_mod.get_card(conn, cid) is not None


def test_delete_removes_an_unreferenced_card(client, conn):
    from app import db as db_mod
    cid = client.post("/api/cards", json={"name": "Typo", "kind": "debit"}).json()["id"]
    assert client.delete(f"/api/cards/{cid}").json()["archived"] is False
    assert db_mod.get_card(conn, cid) is None


def test_breakdown_buckets_unassigned(client, conn):
    from app import db as db_mod
    cid = client.post("/api/cards", json={"name": "Trust Link",
                                          "kind": "debit"}).json()["id"]
    db_mod.add_transaction(conn, 1000, "a", "Food", "trust-email",
                           created_at="2026-08-02T10:00:00", card_id=cid)
    db_mod.add_transaction(conn, 250, "b", "Food", "telegram",
                           created_at="2026-08-03T10:00:00")
    buckets = client.get("/api/cards/breakdown?month=2026-08").json()["buckets"]
    assert {b["card_id"]: b["total_cents"] for b in buckets} == {cid: 1000, None: 250}
    assert [b["name"] for b in buckets if b["card_id"] is None] == [None]


def test_patch_updates_fields(client):
    cid = client.post("/api/cards", json={"name": "Old", "kind": "debit"}).json()["id"]
    r = client.patch(f"/api/cards/{cid}", json={"name": "New", "kind": "credit"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "New"
    assert body["kind"] == "credit"


def test_patch_validates_kind(client):
    cid = client.post("/api/cards", json={"name": "X", "kind": "debit"}).json()["id"]
    r = client.patch(f"/api/cards/{cid}", json={"kind": "cryptocurrency"})
    assert r.status_code == 400


def test_patch_404s_on_unknown_card(client):
    r = client.patch("/api/cards/999999", json={"name": "X"})
    assert r.status_code == 404


def test_delete_404s_on_unknown_card(client):
    r = client.delete("/api/cards/999999")
    assert r.status_code == 404


def test_patching_a_card_learns_the_instrument(client, conn):
    from app import cards as cards_mod, db as db_mod
    cid = client.post("/api/cards", json={"name": "Trust Link",
                                          "kind": "debit"}).json()["id"]
    tid = db_mod.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                                 instrument="trust link card")
    r = client.patch(f"/api/transactions/{tid}", json={"card_id": cid})
    assert r.status_code == 200 and r.json()["card_id"] == cid
    assert cards_mod.effective_card(conn, "trust-email", "trust link card") == \
        (cid, "memory")


def test_an_amount_only_edit_leaves_the_card_alone(client, conn):
    # saveEdit() always sends every field, so `card_id is not None` is dead
    # code -- the comparison must be against the PRE-update row. Without it an
    # unrelated edit re-learns or wipes the attribution. Already fixed once for
    # merchant memory; this is the same bug in a new column.
    from app import cards as cards_mod, db as db_mod
    a = client.post("/api/cards", json={"name": "A", "kind": "debit"}).json()["id"]
    tid = db_mod.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                                 instrument="trust link card", card_id=a)
    client.patch(f"/api/transactions/{tid}", json={"amount": 7.50, "card_id": a})
    # No correction happened, so nothing was taught.
    assert cards_mod.effective_card(conn, "trust-email", "trust link card") == \
        (None, None)


def test_clearing_a_card_forgets_the_binding(client, conn):
    from app import cards as cards_mod, db as db_mod
    cid = client.post("/api/cards", json={"name": "A", "kind": "debit"}).json()["id"]
    tid = db_mod.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                                 instrument="trust link card")
    client.patch(f"/api/transactions/{tid}", json={"card_id": cid})
    client.patch(f"/api/transactions/{tid}", json={"card_id": 0})
    assert db_mod.get_transaction(conn, tid)["card_id"] is None
    assert cards_mod.effective_card(conn, "trust-email", "trust link card") == \
        (None, None)


def test_an_unknown_card_id_is_rejected(client, conn):
    """The 400 must be raised BEFORE either write -- update_transaction commits,
    so validating afterwards would persist the amount and a dangling card_id
    and only then report failure."""
    from app import db as db_mod
    tid = db_mod.add_transaction(conn, 500, "kopi", "Food", "trust-email")
    assert client.patch(f"/api/transactions/{tid}",
                        json={"amount": 9.99, "card_id": 9999}).status_code == 400
    after = db_mod.get_transaction(conn, tid)
    assert after["card_id"] is None
    assert after["amount_cents"] == 500


def test_an_archived_card_cannot_be_assigned(client, conn):
    """recall_instrument joins on archived_at IS NULL, so binding an instrument
    to an archived card would learn something that can never be recalled."""
    from app import db as db_mod
    cid = client.post("/api/cards", json={"name": "Old", "kind": "debit"}).json()["id"]
    tid = db_mod.add_transaction(conn, 500, "kopi", "Food", "trust-email")
    db_mod.archive_card(conn, cid)
    assert client.patch(f"/api/transactions/{tid}",
                        json={"card_id": cid}).status_code == 400
    assert db_mod.get_transaction(conn, tid)["card_id"] is None


def test_patching_only_the_card_returns_200_not_404(client, conn):
    """A card-only PATCH sends no amount/note/category, so update_transaction
    alone reports "nothing to update" -- the route must still succeed via
    assign_card."""
    from app import db as db_mod
    cid = client.post("/api/cards", json={"name": "A", "kind": "debit"}).json()["id"]
    tid = db_mod.add_transaction(conn, 500, "kopi", "Food", "telegram")
    r = client.patch(f"/api/transactions/{tid}", json={"card_id": cid})
    assert r.status_code == 200
    assert r.json()["card_id"] == cid


def test_the_payload_lists_the_bindable_senders(client):
    """The binding picker's options come from the parser map, so a parser added
    to trust_email.SOURCES is bindable with no edit here or in the JS.

    Compared against the module rather than six typed names -- Task 11 adds one.
    """
    from app import trust_email
    want = sorted((label, src) for label, src in trust_email.SOURCES.values())
    got = client.get("/api/cards").json()["sources"]
    assert [(s["label"], s["value"]) for s in got] == want


def test_a_bound_sender_names_the_card_that_owns_it(client):
    """Without this the UI cannot warn before a rebind, and rebinding moves
    every future import from that sender."""
    cid = client.post("/api/cards", json={"name": "Trust Link", "kind": "debit",
                                          "bind_source": "trust-email"}).json()["id"]
    by = {s["value"]: s["card_id"] for s in client.get("/api/cards").json()["sources"]}
    assert by["trust-email"] == cid
    assert by["grab-email"] is None


def test_only_the_source_level_binding_names_the_owner(client, conn):
    """A per-instrument binding is a LEARNED correction (precedence 1), not the
    sender's default (precedence 3). Reporting one as the owner would warn
    about moving an attribution that was never source-level, and which card got
    named would fall out of list_instruments' ORDER BY hits DESC."""
    from app import db as db_mod
    cid = client.post("/api/cards", json={"name": "A", "kind": "debit"}).json()["id"]
    db_mod.bind_instrument(conn, "trust-email", "**** 1234", cid)
    by = {s["value"]: s["card_id"] for s in client.get("/api/cards").json()["sources"]}
    assert by["trust-email"] is None


def test_listing_cards_does_not_bump_the_binding_hit_count(client, conn):
    """The hit count is how an over-broad default shows up as a runaway number.
    The dashboard re-fetches /api/cards on every refresh, so reading the
    binding through db.recall_instrument (which bumps) would destroy it."""
    from app import db as db_mod
    client.post("/api/cards", json={"name": "A", "kind": "debit",
                                    "bind_source": "trust-email"})
    for _ in range(3):
        client.get("/api/cards")
    assert [r["hits"] for r in db_mod.list_instruments(conn) if r["token"] == ""] == [0]


def test_a_binding_to_an_archived_card_reports_as_unbound(client, conn):
    """recall_instrument joins on archived_at IS NULL, so such a binding never
    resolves. Naming the archived card as the owner would warn about moving an
    attribution that is not in force."""
    from app import db as db_mod
    cid = client.post("/api/cards", json={"name": "Old", "kind": "debit",
                                          "bind_source": "trust-email"}).json()["id"]
    db_mod.archive_card(conn, cid)
    by = {s["value"]: s["card_id"] for s in client.get("/api/cards").json()["sources"]}
    assert by["trust-email"] is None


def test_patch_rebinds_a_sender_to_another_card(client, conn):
    """The user's stated scenario: Trust mail is the debit card today and the
    credit card from next month. Without bind_source on CardPatch the only way
    to move it is hand-edited SQLite."""
    from app import cards as cards_mod
    debit = client.post("/api/cards", json={"name": "Trust debit", "kind": "debit",
                                            "bind_source": "trust-email"}).json()["id"]
    credit = client.post("/api/cards", json={"name": "Trust credit",
                                             "kind": "credit"}).json()["id"]
    assert client.patch(f"/api/cards/{credit}",
                        json={"bind_source": "trust-email"}).status_code == 200
    assert cards_mod.effective_card(conn, "trust-email", "anything") == (credit, "source")
    assert debit != credit


def test_patch_rejects_an_unknown_bind_source(client, conn):
    """...and rejects it BEFORE writing anything: a 400 that already renamed
    the card reports failure for a change that happened."""
    from app import db as db_mod
    cid = client.post("/api/cards", json={"name": "X", "kind": "debit"}).json()["id"]
    r = client.patch(f"/api/cards/{cid}", json={"name": "New",
                                                "bind_source": "not-a-sender"})
    assert r.status_code == 400
    assert db_mod.get_card(conn, cid)["name"] == "X"


def test_a_match_rule_is_cleared_with_an_empty_string(client, conn):
    """CardPatch reads None as "not supplied", so "" is the only way to clear a
    rule -- normalise_instrument("") is falsy, so the rule goes inert. A
    truthiness filter on the patch fields would silently drop it and make a
    typed-and-regretted rule permanent."""
    from app import cards as cards_mod
    cid = client.post("/api/cards", json={"name": "A", "kind": "debit",
                                          "match": "trust"}).json()["id"]
    assert cards_mod.effective_card(conn, "trust-email", "trust link card") == (cid, "rule")
    assert client.patch(f"/api/cards/{cid}", json={"match": ""}).json()["match"] == ""
    assert cards_mod.effective_card(conn, "trust-email", "trust link card") == (None, None)
