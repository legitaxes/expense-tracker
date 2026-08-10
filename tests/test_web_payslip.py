"""Payslip web routes: gating, shape, and the auth boundary.

These read what the bot archived; nothing here decrypts or writes.
"""
import pytest
from fastapi.testclient import TestClient

from app import auth, db, web

ITEMS = [
    {"section": "EARNINGS", "label": "Basic Pay", "amount_cents": 450000,
     "ordinal": 0},
    {"section": "DEDUCTIONS", "label": "CPF Employee", "amount_cents": -80000,
     "ordinal": 1},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbfile = str(tmp_path / "t.db")
    monkeypatch.setattr(web, "DB_PATH", dbfile)
    monkeypatch.setattr(web, "SIGNUP_CODE", "")
    # Non-empty password = feature configured. No decryption happens in these
    # routes; the value only decides whether the tab is offered at all.
    monkeypatch.setattr(web, "PAYSLIP_PDF_PASSWORD", "Pay01011999")
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
        c.tmp = tmp_path
        yield c
    web.app.dependency_overrides.clear()
    conn.close()


def test_reports_unconfigured_when_no_password(client, monkeypatch):
    # The frontend hides the tab off this, so it must be a 200 with a flag —
    # a 4xx would be indistinguishable from a real outage.
    monkeypatch.setattr(web, "PAYSLIP_PDF_PASSWORD", "")
    r = client.get("/api/payslips")
    assert r.status_code == 200
    assert r.json() == {"configured": False, "payslips": []}


def test_lists_archived_payslips_newest_first(client):
    for period in ("2026-06", "2026-07"):
        db.upsert_payslip(client.conn, period, f"/data/{period}.pdf", "text",
                          ITEMS, net_cents=415750)
    body = client.get("/api/payslips").json()
    assert body["configured"] is True
    assert [p["period"] for p in body["payslips"]] == ["2026-07", "2026-06"]


def test_breakdown_returns_items_in_document_order(client):
    db.upsert_payslip(client.conn, "2026-07", "/data/x.pdf", "text", ITEMS,
                      net_cents=415750, gross_cents=497000)
    body = client.get("/api/payslips/2026-07").json()
    assert body["parsed"] is True
    assert [i["label"] for i in body["items"]] == ["Basic Pay", "CPF Employee"]
    assert body["items"][1]["amount_cents"] == -80000


def test_unparsed_payslip_is_flagged_not_hidden(client):
    # Decrypt failure archives the file with empty raw_text. The month must
    # still list and open, with the UI able to say it could not be read.
    db.upsert_payslip(client.conn, "2026-08", "/data/y.pdf", "", [])
    body = client.get("/api/payslips/2026-08").json()
    assert body["parsed"] is False
    assert body["items"] == []


def test_breakdown_exposes_the_raw_text_and_its_source(client):
    # The dashboard offers the original PDF and the raw text beside the
    # extracted figures, so a misread digit can be checked against the source.
    db.upsert_payslip(client.conn, "2026-07", "/data/x.pdf", "RAW TEXT", ITEMS,
                      text_source="ocr")
    body = client.get("/api/payslips/2026-07").json()
    assert body["raw_text"] == "RAW TEXT"
    assert body["text_source"] == "ocr"


def test_breakdown_reports_whether_the_figures_reconcile(client):
    # gross - deductions == net, derived on read from the stored items rather
    # than trusted from ingest time.
    items = [
        {"section": "Deductions", "label": "Total", "amount_cents": 80300,
         "ordinal": 0},
    ]
    db.upsert_payslip(client.conn, "2026-07", "/data/x.pdf", "t", items,
                      net_cents=371700, gross_cents=452000)
    assert client.get("/api/payslips/2026-07").json()["reconciles"] is True

    db.upsert_payslip(client.conn, "2026-08", "/data/y.pdf", "t", items,
                      net_cents=371700, gross_cents=999999)
    assert client.get("/api/payslips/2026-08").json()["reconciles"] is False


def test_missing_period_404s(client):
    assert client.get("/api/payslips/1999-01").status_code == 404


def test_pdf_route_serves_the_archived_file(client):
    pdf = client.tmp / "2026-07.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    db.upsert_payslip(client.conn, "2026-07", str(pdf), "text", ITEMS)
    r = client.get("/api/payslips/2026-07/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == b"%PDF-1.4 fake"


def test_pdf_route_404s_when_the_file_vanished(client):
    # Row present, file gone (restored DB, pruned dataset). Must not 500.
    db.upsert_payslip(client.conn, "2026-07", "/nope/missing.pdf", "text", ITEMS)
    assert client.get("/api/payslips/2026-07/pdf").status_code == 404


# --- import: the web equivalents of the bot's /payslip ---------------------

@pytest.fixture
def mailbox(client, monkeypatch, tmp_path):
    """Configure the email path and stub the mailbox. Returns a setter."""
    monkeypatch.setattr(web, "GMAIL_USER", "me@gmail.com")
    monkeypatch.setattr(web, "GMAIL_APP_PASSWORD", "app-pw")
    monkeypatch.setattr(web, "PAYSLIP_SENDER", "payroll@example.com")
    monkeypatch.setattr(web, "PAYSLIP_DIR", str(tmp_path / "payslips"))
    calls = []

    def install(emails):
        def fake(host, user, pw, sender, limit):
            calls.append({"sender": sender, "limit": limit})
            return 42, emails
        monkeypatch.setattr(web.trust_email, "fetch_from_sender", fake)

    install.calls = calls
    return install


def _stub_archive(monkeypatch, results):
    seen = []

    def fake(conn, data, password, dest, fallback, source_uid=None,
             received_at=None):
        seen.append({"data": data, "password": password, "fallback": fallback,
                     "uid": source_uid})
        return results
    monkeypatch.setattr(web.payslip_archive, "archive_pdf", fake)
    return seen


_OK = [{"period": "2026-07", "parsed": True, "items": [{"a": 1}],
        "net_cents": 250030, "gross_cents": 315980, "text_source": "ocr",
        "reconciles": True, "warnings": []}]


def test_import_refuses_when_the_payslip_password_is_unset(client, monkeypatch):
    # Must not reach the mailbox at all — same gating as the bot.
    monkeypatch.setattr(web, "PAYSLIP_PDF_PASSWORD", "")
    monkeypatch.setattr(web.trust_email, "fetch_from_sender",
                        lambda *a, **k: pytest.fail("must not touch the mailbox"))
    r = client.post("/api/payslips/import")
    assert r.status_code == 400
    assert "PAYSLIP_PDF_PASSWORD" in r.json()["detail"]


def test_import_refuses_when_email_access_is_unset(client, mailbox, monkeypatch):
    monkeypatch.setattr(web, "GMAIL_APP_PASSWORD", "")
    r = client.post("/api/payslips/import")
    assert r.status_code == 400
    assert "GMAIL_APP_PASSWORD" in r.json()["detail"]


def test_import_archives_each_attached_pdf(client, mailbox, monkeypatch):
    from datetime import datetime
    dt = datetime(2026, 7, 31, 9, 0)
    mailbox([(7, "payroll@example.com", "s", "", dt, [("a.pdf", b"%PDF a")])])
    seen = _stub_archive(monkeypatch, _OK)
    body = client.post("/api/payslips/import").json()
    assert body["scanned"] == 1
    assert body["imported"][0]["period"] == "2026-07"
    # The email's month is only the FALLBACK; the real month comes from inside
    # the document. And the source UID is recorded for traceability.
    assert seen[0]["fallback"] == "2026-07" and seen[0]["uid"] == 7


def test_import_skips_payroll_mail_with_no_pdf(client, mailbox, monkeypatch):
    from datetime import datetime
    mailbox([(7, "payroll@example.com", "s", "", datetime(2026, 7, 31), [])])
    _stub_archive(monkeypatch, _OK)
    body = client.post("/api/payslips/import").json()
    assert body["scanned"] == 1
    assert body["imported"] == []


def test_import_searches_by_sender_and_clamps_the_scan_depth(
        client, mailbox, monkeypatch):
    mailbox([])
    for sent, expected in ((None, 3), (99, 20), (0, 1), (5, 5)):
        url = "/api/payslips/import"
        if sent is not None:
            url += f"?limit={sent}"
        client.post(url)
        assert mailbox.calls[-1]["limit"] == expected
    assert mailbox.calls[-1]["sender"] == "payroll@example.com"


def test_import_reports_a_mailbox_failure_as_503(client, mailbox, monkeypatch):
    def boom(*a, **k):
        raise OSError("connection reset")
    monkeypatch.setattr(web.trust_email, "fetch_from_sender", boom)
    r = client.post("/api/payslips/import")
    assert r.status_code == 503
    assert "OSError" in r.json()["detail"]


def test_import_never_returns_line_items(client, mailbox, monkeypatch):
    # The Pay tab re-fetches whichever month it lands on, so shipping the items
    # in the import response too would be pure waste.
    from datetime import datetime
    mailbox([(7, "payroll@example.com", "s", "", datetime(2026, 7, 31),
              [("a.pdf", b"%PDF a")])])
    _stub_archive(monkeypatch, _OK)
    row = client.post("/api/payslips/import").json()["imported"][0]
    assert row["items"] == 1  # a count, not the list
    assert "raw_text" not in row


# --- upload ----------------------------------------------------------------

def test_upload_archives_a_pdf_handed_over_directly(client, monkeypatch,
                                                    tmp_path):
    monkeypatch.setattr(web, "PAYSLIP_DIR", str(tmp_path / "payslips"))
    seen = _stub_archive(monkeypatch, _OK)
    r = client.post("/api/payslips/upload", content=b"%PDF-1.4 payslip",
                    headers={"Content-Type": "application/pdf"})
    assert r.status_code == 200
    assert r.json()["imported"][0]["period"] == "2026-07"
    assert seen[0]["data"] == b"%PDF-1.4 payslip"
    # No email behind it, so nothing to attribute it to.
    assert seen[0]["uid"] is None


def test_upload_rejects_something_that_is_not_a_pdf(client, monkeypatch):
    monkeypatch.setattr(web.payslip_archive, "archive_pdf",
                        lambda *a, **k: pytest.fail("must not parse"))
    r = client.post("/api/payslips/upload", content=b"PK\x03\x04 a zip file")
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_upload_refuses_an_oversized_body(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_PAYSLIP_UPLOAD", 1024)
    monkeypatch.setattr(web.payslip_archive, "archive_pdf",
                        lambda *a, **k: pytest.fail("must not parse"))
    r = client.post("/api/payslips/upload", content=b"%PDF" + b"x" * 4096)
    assert r.status_code == 413


def test_upload_refuses_when_the_payslip_password_is_unset(client, monkeypatch):
    monkeypatch.setattr(web, "PAYSLIP_PDF_PASSWORD", "")
    monkeypatch.setattr(web.payslip_archive, "archive_pdf",
                        lambda *a, **k: pytest.fail("must not parse"))
    r = client.post("/api/payslips/upload", content=b"%PDF-1.4 x")
    assert r.status_code == 400


def test_upload_ignores_any_password_sent_over_the_wire(client, monkeypatch,
                                                        tmp_path):
    """The decrypt password comes from the environment, never the request.

    Accepting one over HTTP would put a payslip password in proxy logs and
    browser history, and would let anyone past the session gate try candidates.
    """
    monkeypatch.setattr(web, "PAYSLIP_DIR", str(tmp_path / "p"))
    seen = _stub_archive(monkeypatch, _OK)
    client.post("/api/payslips/upload?password=guess", content=b"%PDF-1.4 x",
                headers={"X-Password": "guess"})
    # The configured password was used; the ones on the request were ignored.
    assert seen[0]["password"] == "Pay01011999"


def test_payslip_routes_require_a_session(client):
    """401 JSON, never a 302 to the login page.

    These live under /api/* precisely so the gate answers with a status a
    fetch() can act on. A redirect would be followed transparently and the
    login HTML parsed as JSON — or handed back with a PDF content type.
    """
    db.upsert_payslip(client.conn, "2026-07", "/data/x.pdf", "text", ITEMS)
    client.cookies.clear()
    for path in ("/api/payslips", "/api/payslips/2026-07",
                 "/api/payslips/2026-07/pdf"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 401, path
    # The import routes especially: they read a mailbox and write to disk.
    for path in ("/api/payslips/import", "/api/payslips/upload"):
        r = client.post(path, content=b"%PDF-1.4 x", follow_redirects=False)
        assert r.status_code == 401, path
