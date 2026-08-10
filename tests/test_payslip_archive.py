"""The archive step shared by the bot and the dashboard.

This module exists so both front ends run the same code; these tests pin the
behaviour that used to live inside the bot — where the file lands, what gets
written to the DB, and which findings are worth warning about.
"""
import pytest

from app import db, payslip_archive
from tests.test_payslip import PAGE_BREAK, _encrypted_pdf

PW = "Pay01011999"
# A section heading only starts a section when the column-header row follows
# it, exactly as the real document prints it.
HDR = "Serial No. Description Amount ($)"


def _payslip_text(net="2,500.00"):
    return ("June 2026\nSalary/ Allowances\n" + HDR + "\n"
            "1 BASIC COMPONENT 3,000.00\n"
            "Total Gross Pay 3,000.00\n"
            "Deductions\n" + HDR + "\n"
            "1 CPF 500.00\nTotal 500.00\n"
            "Net Pay " + net)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def dest(tmp_path):
    return str(tmp_path / "payslips")


def test_writes_one_pdf_per_month_named_for_its_period(conn, dest, tmp_path):
    june = "June 2026\nSerial No. Description Amount ($)\n1 BASIC 3,000.00"
    july = "July 2026\nSerial No. Description Amount ($)\n1 BASIC 3,100.00"
    pdf = _encrypted_pdf(PW, june + "\n" + PAGE_BREAK + "\n" + july)

    results = payslip_archive.archive_pdf(conn, pdf, PW, dest, "2026-01")

    assert [r["period"] for r in results] == ["2026-06", "2026-07"]
    files = sorted(p.name for p in (tmp_path / "payslips").iterdir())
    assert files == ["2026-06.pdf", "2026-07.pdf"]
    # Each file holds only its own month's pages, not the whole document.
    assert [r["period"] for r in db.list_payslips(conn)] == ["2026-07", "2026-06"]


def test_creates_the_destination_directory(conn, tmp_path):
    nested = str(tmp_path / "a" / "b" / "payslips")
    payslip_archive.archive_pdf(conn, _encrypted_pdf(PW), PW, nested, "2026-01")
    assert (tmp_path / "a" / "b" / "payslips").is_dir()


def test_reimporting_a_month_replaces_it(conn, dest):
    pdf = _encrypted_pdf(PW)
    payslip_archive.archive_pdf(conn, pdf, PW, dest, "2026-01")
    payslip_archive.archive_pdf(conn, pdf, PW, dest, "2026-01")
    # Both front ends can be run repeatedly; a second import must not stack up
    # a duplicate month or duplicate line items.
    rows = db.list_payslips(conn)
    assert len(rows) == 1
    items = db.payslip_items(conn, rows[0]["id"])
    labels = [i["label"] for i in items]
    assert labels == sorted(set(labels), key=labels.index)  # no duplicates


def test_a_bad_password_archives_the_file_and_says_so(conn, dest, tmp_path):
    # Losing the document is worse than failing to read it: the bytes are kept
    # and the month lists as unparsed rather than vanishing.
    results = payslip_archive.archive_pdf(
        conn, _encrypted_pdf(PW), "wrong-password", dest, "2026-08")
    assert len(results) == 1
    assert results[0]["period"] == "2026-08"
    assert results[0]["parsed"] is False
    assert "PAYSLIP_PDF_PASSWORD" in results[0]["warnings"][0]
    assert (tmp_path / "payslips" / "2026-08.pdf").is_file()
    assert db.get_payslip(conn, "2026-08") is not None


def test_warns_when_the_pay_month_had_to_be_guessed(conn, dest):
    # A payslip covers the month BEFORE it is sent, so a fallback month is
    # probably wrong. It must be flagged, not filed quietly.
    no_heading = "Serial No. Description Amount ($)\n1 BASIC 3,000.00"
    [r] = payslip_archive.archive_pdf(
        conn, _encrypted_pdf(PW, no_heading), PW, dest, "2026-08")
    assert r["period"] == "2026-08"
    assert any("Couldn't read the pay month" in w for w in r["warnings"])


def test_warns_when_the_figures_do_not_reconcile(conn, dest):
    [r] = payslip_archive.archive_pdf(
        conn, _encrypted_pdf(PW, _payslip_text(net="9,999.00")), PW, dest,
        "2026-01")
    assert r["reconciles"] is False
    assert any("doesn't equal net" in w for w in r["warnings"])


def test_a_clean_payslip_carries_no_warnings(conn, dest):
    [r] = payslip_archive.archive_pdf(
        conn, _encrypted_pdf(PW, _payslip_text()), PW, dest, "2026-01")
    assert r["reconciles"] is True
    assert r["warnings"] == []
    assert r["parsed"] is True


def test_records_the_source_uid_and_receipt_time_when_given(conn, dest):
    payslip_archive.archive_pdf(conn, _encrypted_pdf(PW), PW, dest, "2026-01",
                                source_uid=4242,
                                received_at="2026-07-31T09:00:00")
    row = db.get_payslip(conn, db.list_payslips(conn)[0]["period"])
    assert row["source_uid"] == 4242
    assert row["received_at"] == "2026-07-31T09:00:00"


def test_an_upload_has_no_uid_and_still_archives(conn, dest):
    # The dashboard's upload path passes neither; the columns are nullable and
    # the month must archive exactly the same way.
    [r] = payslip_archive.archive_pdf(conn, _encrypted_pdf(PW), PW, dest,
                                      "2026-01")
    assert r["parsed"] is True
    period = db.list_payslips(conn)[0]["period"]
    assert db.get_payslip(conn, period)["source_uid"] is None


def test_no_transaction_is_ever_written(conn, dest):
    # The bank credit alert is already the salary income row; a row here would
    # double-count the month. True for every entry point, so it is asserted at
    # the shared layer rather than per front end.
    payslip_archive.archive_pdf(conn, _encrypted_pdf(PW), PW, dest, "2026-01")
    assert db.list_transactions(conn) == []
