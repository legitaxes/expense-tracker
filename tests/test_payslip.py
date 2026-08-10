"""Payslip parsing + archive tests.

Both fixtures are SYNTHETIC. PAYROLL_LAYOUT copies the *structure* of a real
payslip (verified against one on 2026-08-06) but every figure, name and
identifier is invented. No real payroll data enters this repo.
"""
import io
import sqlite3

import pytest

from app import db, payslip, trust_email

# A payslip-shaped document: section headings carry no figure, item lines end
# in one. Deliberately includes a label whose wording matches neither hint list
# ("Transport Allowance") and a DR-marked deduction.
SAMPLE = """
MONTHLY PAYSLIP
For the month of July 2026

EARNINGS
Basic Pay                      4,500.00
Service Allowance                350.00
Transport Allowance              120.00
Total Earnings                 4,970.00

DEDUCTIONS
CPF Employee                     800.00 DR
Group Insurance                   12.50 DR
Total Deductions                 812.50

Net Pay                        4,157.50

EMPLOYER CONTRIBUTION
CPF Employer                     850.00
"""


# Structure of a real payslip; all values invented. The traps it encodes,
# all of which the first real sample exposed:
#   - the pay month is a standalone heading, and is the month BEFORE the one
#     the email arrives in
#   - "Gross Pay" and "Total Gross Pay" are DIFFERENT figures
#   - a year-to-date line repeats the "Total Gross Pay" wording
#   - adjustment rows name OTHER months in their description
#   - rows are numbered and may carry a "*" marker
#   - personal-detail rows ("Rank/Name : ...") carry no figure
PAYROLL_LAYOUT = """
7/31/26, 9:55 AM e-Statements - Payslip
EXAMPLE PAYROLL SERVICES PAYSLIP
OFFICIAL (CLOSED) \\ INTERNAL
June 2026
Rank/Name : GRADE1 EXAMPLE PERSON
Payment Date : 10 Jun 2026
NRIC No. : S0000000A
Payroll Period: 1st to last day of the month
Salary/Allowances
Serial No. Description Amount ($)
1 BASIC COMPONENT 3,000.00
2 MVC 500.00
3 * TRAINEE PAY (ADJUSTMENT) (May 2026) 1,000.00
4 * Allw- Mobile & Broadband 20.00
Total $ 4,520.00
Deductions
Serial No. Description Amount ($)
1 CPF EMPLOYEE 700.00
2 * CPF EMPLOYEE (ADJUSTMENT) (May 2026) 100.00
3 COMMUNITY CHEST 3.00
Total $ 803.00
Gross Pay : $ 3,500.00
Net Pay : $ 3,717.00
Total Gross Pay : $ 4,520.00
CPF Employer : $ 600.00
Total Gross Pay for Year 2026 (up to current month) = $ 9,999.00
This is a computer generated pay slip.
"""


def test_parse_period_from_month_name():
    assert payslip.parse_period(SAMPLE, "2026-01") == ("2026-07", "document")


def test_parse_period_from_numeric_pay_period():
    text = "Pay Period: 01/09/2026 - 30/09/2026"
    assert payslip.parse_period(text, "2026-01") == ("2026-09", "document")


def test_parse_period_reports_when_it_fell_back():
    # The caller must be told: a payslip covers the month BEFORE the one it is
    # emailed in, so the email's month is a wrong answer, not a rough one.
    assert payslip.parse_period("no date here", "2026-05") == ("2026-05", "fallback")


# ---- real payroll layout -----------------------------------------------------

def test_payroll_period_is_the_heading_not_the_email_month():
    """June payslip, emailed 31 July -> must be 2026-06, never 2026-07."""
    period, source = payslip.parse_period(PAYROLL_LAYOUT, "2026-07")
    assert (period, source) == ("2026-06", "document")


def test_payroll_period_ignores_adjustment_and_ytd_months():
    # "(May 2026)" and "for Year 2026" both appear; neither is the pay month.
    assert payslip.parse_period(PAYROLL_LAYOUT, "2026-07")[0] == "2026-06"


def test_payroll_gross_prefers_total_gross_over_gross_pay():
    """The two differ, and only Total Gross Pay reconciles with Net Pay."""
    got = payslip.summarize(payslip.parse_items(PAYROLL_LAYOUT))
    assert got["gross_cents"] == 452000   # Total Gross Pay, not the 3,500 line
    assert got["net_cents"] == 371700


def test_payroll_gross_ignores_the_year_to_date_line():
    items = payslip.parse_items(PAYROLL_LAYOUT)
    assert payslip.summarize(items)["gross_cents"] != 999900


def test_payroll_totals_reconcile():
    items = payslip.parse_items(PAYROLL_LAYOUT)
    got = payslip.summarize(items)
    # 4,520.00 - 803.00 = 3,717.00
    assert payslip.reconciles(got["net_cents"], got["gross_cents"], items) is True


def test_reconciles_catches_a_wrong_figure():
    items = payslip.parse_items(PAYROLL_LAYOUT)
    assert payslip.reconciles(371700, 999900, items) is False


def test_payroll_row_numbers_and_markers_are_stripped():
    labels = [i["label"] for i in payslip.parse_items(PAYROLL_LAYOUT)]
    assert "BASIC COMPONENT" in labels
    assert "TRAINEE PAY (ADJUSTMENT) (May 2026)" in labels
    assert not any(l.startswith(("1 ", "*", "2 ")) for l in labels)


def test_payroll_column_headers_never_become_sections():
    sections = {i["section"] for i in payslip.parse_items(PAYROLL_LAYOUT)}
    assert "Salary/Allowances" in sections
    assert "Deductions" in sections
    assert not any("Serial No" in s or "Description" in s for s in sections)


def test_payroll_personal_detail_rows_never_become_sections():
    # "Rank/Name : ..." and "NRIC No. : ..." carry no figure; if one became a
    # section every following row would be filed under someone's name.
    sections = {i["section"] for i in payslip.parse_items(PAYROLL_LAYOUT)}
    assert not any("Rank" in s or "NRIC" in s or "Payment" in s for s in sections)


def test_payroll_summary_pairs_on_one_line_are_both_kept():
    # "Gross Pay : $ X   Total Gross Pay : $ Y" is ONE line with TWO pairs; an
    # end-anchored match would fold the first label and figure into the second.
    items = {i["label"]: i["amount_cents"] for i in payslip.parse_items(
        "Gross Pay : $ 3,500.00 Total Gross Pay : $ 4,520.00")}
    assert items == {"Gross Pay": 350000, "Total Gross Pay": 452000}


def test_payroll_wrapped_description_joins_the_row_above():
    items = payslip.parse_items(
        "Serial No. Description Amount ($)\n"
        "4 * CDAC (ADJUSTMENT) 1.50\n(May 2026)\n")
    assert items[0]["label"] == "CDAC (ADJUSTMENT) (May 2026)"


def test_ocr_misread_star_marker_is_still_stripped():
    # Tesseract reads the "*" adjustment marker as "x" on some rows; without
    # this the marker would be kept as the first word of the description.
    items = payslip.parse_items(
        "Serial No. Description Amount ($)\n5 x CDAC (ADJUSTMENT) 0.50")
    assert items[0]["label"] == "CDAC (ADJUSTMENT)"


def test_page_chrome_never_glues_onto_a_label():
    # The browser print header appears mid-document at every page break and
    # begins with a timestamp, so an anchored rule misses it.
    items = payslip.parse_items(
        "CPF Employer : $ 649.00\n7/31/26, 9:55 AM e-Statements - Payslip\n")
    assert items[0]["label"] == "CPF Employer"


def test_ytd_rows_do_not_inherit_the_deductions_section():
    # They sit after the last table with no header of their own; inheriting
    # "Deductions" reads as if the year's gross pay had been deducted.
    items = payslip.parse_items(PAYROLL_LAYOUT)
    ytd = [i for i in items if "for Year" in i["label"]]
    assert ytd and all(i["section"] == "Year to date" for i in ytd)


def test_identical_months_with_identical_figures_are_both_kept():
    """A steady salary makes two months look identical — neither may vanish.

    Dedup keys on (period, figures). Keying on figures alone silently dropped
    the second month, which is the normal case for an unchanging salary.
    """
    june = "June 2026\nSerial No. Description Amount ($)\n1 BASIC 10.00"
    july = june.replace("June 2026", "July 2026")
    two = _encrypted_pdf("pw", june + "\n" + PAGE_BREAK + "\n" + july)
    slips = payslip.parse_pdf(two, "pw", "2026-01")
    assert [s["period"] for s in slips] == ["2026-06", "2026-07"]


def test_the_same_payslip_printed_twice_collapses_to_one():
    june = "June 2026\nSerial No. Description Amount ($)\n1 BASIC 10.00"
    twice = _encrypted_pdf("pw", june + "\n" + PAGE_BREAK + "\n" + june)
    slips = payslip.parse_pdf(twice, "pw", "2026-01")
    assert [s["period"] for s in slips] == ["2026-06"]


def test_split_payslips_groups_pages_by_month_heading():
    pages = ["June 2026\nBasic 1.00", "continuation page",
             "July 2026\nBasic 2.00", "continuation page 2"]
    assert payslip.split_payslips(pages) == [
        ("2026-06", [0, 1]), ("2026-07", [2, 3])]


def test_split_payslips_returns_one_group_for_a_normal_single_month():
    pages = ["June 2026\nBasic 1.00", "page two"]
    assert payslip.split_payslips(pages) == [("2026-06", [0, 1])]


def test_parse_items_captures_labels_and_amounts():
    items = payslip.parse_items(SAMPLE)
    by_label = {i["label"]: i["amount_cents"] for i in items}
    assert by_label["Basic Pay"] == 450000
    assert by_label["Transport Allowance"] == 12000
    assert by_label["Net Pay"] == 415750


def test_parse_items_groups_under_section_headings():
    items = payslip.parse_items(SAMPLE)
    sections = {i["label"]: i["section"] for i in items}
    assert sections["Basic Pay"] == "EARNINGS"
    assert sections["CPF Employee"] == "DEDUCTIONS"
    assert sections["CPF Employer"] == "EMPLOYER CONTRIBUTION"


def test_dr_marker_makes_the_amount_negative():
    items = payslip.parse_items(SAMPLE)
    by_label = {i["label"]: i["amount_cents"] for i in items}
    assert by_label["CPF Employee"] == -80000
    assert by_label["Group Insurance"] == -1250


def test_ordinal_preserves_document_order():
    items = payslip.parse_items(SAMPLE)
    assert [i["ordinal"] for i in items] == list(range(len(items)))
    assert items[0]["label"] == "Basic Pay"


def test_headings_never_become_items():
    labels = [i["label"] for i in payslip.parse_items(SAMPLE)]
    assert "EARNINGS" not in labels
    assert "MONTHLY PAYSLIP" not in labels


def test_bare_numbers_without_a_label_are_ignored():
    assert payslip.parse_items("1,234.00\n  \n99.99") == []


def test_integers_without_decimals_are_not_amounts():
    # Employee numbers, CPF account numbers and dates share the page with the
    # figures; requiring the decimal part is what keeps them out.
    assert payslip.parse_items("Employee Number 12345678") == []


def test_summarize_finds_net_and_gross():
    got = payslip.summarize(payslip.parse_items(SAMPLE))
    assert got["net_cents"] == 415750
    assert got["gross_cents"] == 497000  # "Total Earnings"


def test_summarize_returns_none_when_wording_is_unknown():
    # A payslip that names nothing recognisably still archives in full; only
    # the derived trend columns stay empty.
    items = payslip.parse_items("SECTION\nMystery Line   10.00")
    assert payslip.summarize(items) == {"net_cents": None, "gross_cents": None}


# ---- decrypt (real encrypted PDF, not a stub) ----------------------------
# The parsers above work on text, so on their own they never prove the file can
# actually be opened. These build a genuinely password-protected PDF and push
# it through the same path the email poll uses.

PAGE_BREAK = "===PAGEBREAK==="


def _encrypted_pdf(password: str, text: str = SAMPLE) -> bytes:
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    from pypdf import PdfWriter
    buf = io.BytesIO()
    c = reportlab.Canvas(buf)
    y = 800
    for line in text.strip().splitlines():
        # Not "\f": str.splitlines() treats a form feed as a line separator,
        # so it is consumed before this loop ever sees it.
        if line == PAGE_BREAK:
            c.showPage()
            y = 800
            continue
        c.drawString(50, y, line)
        y -= 16
    c.save()
    writer = PdfWriter(clone_from=io.BytesIO(buf.getvalue()))
    writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_parse_pdf_reads_an_encrypted_payslip():
    slips = payslip.parse_pdf(_encrypted_pdf("Pay01011999"), "Pay01011999",
                              "2026-01")
    assert len(slips) == 1
    got = slips[0]
    assert got["period"] == "2026-07"
    assert got["net_cents"] == 415750
    assert got["text_source"] == "pdf"
    assert any(i["label"] == "Basic Pay" for i in got["items"])


def test_wrong_password_raises_rather_than_returning_empty():
    # Must raise: the caller archives the original bytes and warns. Returning
    # an empty parse would file a blank payslip and say nothing.
    with pytest.raises(ValueError):
        payslip.parse_pdf(_encrypted_pdf("Pay01011999"), "wrong", "2026-01")


def test_archived_copy_opens_without_a_password():
    from pypdf import PdfReader
    slips = payslip.parse_pdf(_encrypted_pdf("Pay01011999"), "Pay01011999",
                              "2026-01")
    assert PdfReader(io.BytesIO(slips[0]["pdf"])).is_encrypted is False


def test_each_payslip_is_archived_with_only_its_own_pages():
    from pypdf import PdfReader
    two = _encrypted_pdf("pw", PAYROLL_LAYOUT + PAGE_BREAK + "\n" + PAYROLL_LAYOUT.replace(
        "June 2026", "July 2026").replace("10 Jun 2026", "10 Jul 2026"))
    slips = payslip.parse_pdf(two, "pw", "2026-01")
    assert [s["period"] for s in slips] == ["2026-06", "2026-07"]
    # Each archived file holds that month's pages only, never the whole PDF.
    total = len(PdfReader(io.BytesIO(payslip.decrypt_pdf(two, "pw"))).pages)
    assert sum(len(PdfReader(io.BytesIO(s["pdf"])).pages) for s in slips) == total
    assert all(len(PdfReader(io.BytesIO(s["pdf"])).pages) < total for s in slips)


# ---- routing -------------------------------------------------------------

# The module default is empty (the payslip feature is off until you set
# PAYSLIP_SENDER), so routing is exercised with an explicit sender -- which is
# also how the bot calls it.
PAYROLL_SENDER = "payroll@example.com"


def _classify(sender, attachments=(), subject="Payslip Request 12345678"):
    from datetime import datetime
    return trust_email.classify_email(sender, subject, "<p>hi</p>",
                                      datetime(2026, 7, 31, 17, 11),
                                      attachments=attachments,
                                      payslip_sender=PAYROLL_SENDER)


def test_payslip_sender_with_pdf_routes_to_payslip():
    action, info = _classify(PAYROLL_SENDER,
                             (("Payslip X 12345678.pdf", b"%PDF-1.4"),))
    assert action == "payslip"
    assert info["data"] == b"%PDF-1.4"


def test_payslip_sender_without_pdf_is_skipped():
    # Payroll correspondence that carries no payslip, or the same mail seen
    # when the feature is disabled (attachments are never decoded then).
    assert _classify(PAYROLL_SENDER) == ("skip", None)


def test_inline_images_are_not_mistaken_for_a_payslip():
    action, _ = _classify(PAYROLL_SENDER,
                          (("image001.jpg", b"\xff\xd8"),))
    assert action == "skip"


def test_payroll_sender_is_not_a_merchant_memory_source():
    # SOURCES is the allowlist merchant-memory learns from. Payroll text is not
    # a merchant, and enrolling it would poison category learning.
    assert trust_email.PAYSLIP_SENDER not in trust_email.SOURCES


# ---- storage -------------------------------------------------------------

@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    yield c
    c.close()


def _items():
    return payslip.parse_items(SAMPLE)


def test_upsert_payslip_stores_items(conn):
    pid = db.upsert_payslip(conn, "2026-07", "/data/2026-07.pdf", SAMPLE,
                            _items(), net_cents=415750)
    rows = db.payslip_items(conn, pid)
    assert len(rows) == len(_items())
    assert rows[0]["label"] == "Basic Pay"


def test_reingesting_a_month_replaces_it(conn):
    """A payslip can be re-requested by hand — July twice must stay one row."""
    db.upsert_payslip(conn, "2026-07", "/data/a.pdf", SAMPLE, _items())
    db.upsert_payslip(conn, "2026-07", "/data/b.pdf", SAMPLE, _items()[:2])
    assert len(db.list_payslips(conn)) == 1
    row = db.get_payslip(conn, "2026-07")
    assert row["pdf_path"] == "/data/b.pdf"
    assert len(db.payslip_items(conn, row["id"])) == 2


def test_list_payslips_is_newest_first(conn):
    for period in ("2026-05", "2026-07", "2026-06"):
        db.upsert_payslip(conn, period, f"/data/{period}.pdf", SAMPLE, [])
    assert [r["period"] for r in db.list_payslips(conn)] == \
        ["2026-07", "2026-06", "2026-05"]


def test_unparsed_payslip_still_archives(conn):
    """Decrypt failure keeps the document: empty raw_text, no items, but a row."""
    db.upsert_payslip(conn, "2026-08", "/data/2026-08.pdf", "", [])
    row = db.get_payslip(conn, "2026-08")
    assert row is not None and row["raw_text"] == ""
    assert db.payslip_items(conn, row["id"]) == []
