"""Payslip PDF ingestion: decrypt, extract text, parse line items.

Pure parsing. No Telegram/DB deps here so the parsers stay unit-testable
against fixture text. The bot wires this into its email poll and does the DB
write + notify; the web dashboard only ever reads what was stored.

Written against one payroll provider's monthly mail, which has the shape:

    From:    payroll@example.com            <- set PAYSLIP_SENDER to yours
    Subject: Payslip Request 12345678       <- request no., NOT the month
    Attach:  Payslip A NAME 12345678.pdf    <- request no., NOT the month
             image001.jpg                   <- signature graphic, ignored
    Body:    a password hint (ours was a fixed prefix + date of birth)

Two consequences drive the shape of this module:

  * The pay month appears in NEITHER the subject nor the filename -- both are
    keyed on a request number that changes every time. `parse_period` reads it
    out of the document text, and the caller supplies the email's own month as
    a fallback.
  * The PDF is encrypted, so nothing here works without the password. An empty
    password disables the whole feature upstream; it is never a repo value.

Item parsing is deliberately GENERIC -- every "<label> ... <amount>" line
becomes a row, grouped under the last section heading seen. The document
layout was not inspectable at build time (encrypted, and no attachment
download available), and a fixed gross/allowance/CPF schema would silently
drop any component that did not fit a column: a bonus month, a back-payment,
a new allowance. Preserving the payslip's own breakdown is also the point of
the feature.
"""
from __future__ import annotations
import io
import re

# Amounts as printed on a payslip: 1,234.56 / 1234.56, optional leading minus
# or a trailing CR/DR marker. The decimal part is REQUIRED -- without it every
# employee number, CPF account and date fragment on the page becomes an
# "amount".
_WS_RE = re.compile(r"\s+")
_AMOUNT = r"-?\$?\s?([\d,]+\.\d{2})"
_ITEM_RE = re.compile(r"^(.*?)\s*[.:\s]\s*" + _AMOUNT + r"\s*(CR|DR)?$", re.I)

# A numbered table row: "3 * BASIC PAY (ADJUSTMENT) (May 2026) 1,234.00".
# The adjustment marker is "*", but OCR reads it as "x" often enough that both
# are accepted -- a marker misread must not swallow the first word of the
# description.
_ROW_RE = re.compile(
    r"^(?:\d{1,3}\s+)?(?:[*x]\s+)?(.+?)\s+\$?\s*(-?[\d,]+\.\d{2})\s*(CR|DR)?$", re.I)

# The summary block prints TWO "label : $ amount" pairs per line:
#   "Gross Pay : $ 1,234.00    Total Gross Pay : $ 2,345.00"
# A single end-anchored match would capture "Gross Pay : $ 1,234.00 Total Gross
# Pay" as one label and drop the first figure entirely.
_PAIR_RE = re.compile(r"([A-Za-z][^:$\d]*?)\s*:\s*\$?\s*(-?[\d,]+\.\d{2})")

# The column-header row that opens each table. The line BEFORE it is that
# table's heading -- a far more reliable signal than "a short line with no
# figure", which also matches wrapped descriptions and logo noise.
_COLHDR_RE = re.compile(r"^serial\s*no\b", re.I)

# The trailing summary block has no column header of its own, so it is
# recognised by its first label instead.
_SUMMARY_RE = re.compile(
    r"^(gross pay|net pay|total gross|cpf employer|medisave|total bonus)", re.I)
_SUMMARY_SECTION = "Summary"
_YTD_SECTION = "Year to date"

# A heading is a short line carrying no amount. Payslips print these in caps
# ("EARNINGS", "DEDUCTIONS", "CPF CONTRIBUTION"), but casing is not relied on
# -- a heading is defined by the absence of a figure, which survives a layout
# that title-cases its headers.
_HEADING_MAX_WORDS = 5

# Period, most specific first. The payslip prints its month as a standalone
# heading ("June 2026") above the tables, which is the strongest signal — an
# adjustment row further down says "(May 2026)" and a year-to-date line says
# "for Year 2026", so a loose scan of the whole document picks the wrong month.
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")
# A line that is ONLY a month and year — the payslip's own title line.
_PERIOD_HEADING_RE = re.compile(r"^([A-Za-z]{3,9})\s+(20\d{2})$")
_PERIOD_RES = (
    # "Payment Date : 10 Jun 2026" — the pay run's own date.
    re.compile(r"payment\s*date\s*:?\s*\d{1,2}\s+([A-Za-z]{3,9})\s+(20\d{2})", re.I),
    # "Pay Period: 01/07/2026 - 31/07/2026" / "01-07-2026"
    re.compile(r"(?:pay\s*period|period|for\s*the\s*month)[^\d]{0,12}"
               r"\d{1,2}[/-](\d{1,2})[/-](\d{4})", re.I),
    # "For the month of July 2026" / "Month: Jul 2026"
    re.compile(r"(?:month|period)\s*(?:of|:)?\s*([A-Za-z]{3,9})\s+(\d{4})", re.I),
    # "07/2026"
    re.compile(r"\b(\d{1,2})/(20\d{2})\b"),
)

# Column headers and boilerplate that carry no figure but must NEVER be taken
# for a section heading — otherwise "Serial No. Description Amount ($)" becomes
# the section every following row is filed under.
_NOISE_RE = re.compile(
    r"^(serial\s*no|description|amount|payroll\s*period|rank/name|nric|"
    r"bank acct|this is a computer generated|no signature|official\s*\(|"
    r"gross pay\s*=|\*one time|https?://|page \d)", re.I)

# Browser print chrome, which lands MID-DOCUMENT at every page break rather
# than only at the top — so this is searched for anywhere in the line, not
# anchored. Unanchored matters: the header line begins with the print
# timestamp ("7/31/26, 9:55 AM e-Statements - Payslip"), so an anchored
# "e-statements" rule misses it and it gets glued onto the previous row.
_PAGE_CHROME_RE = re.compile(
    r"e-?statements|^\d{1,2}/\d{1,2}/\d{2},\s*\d{1,2}:\d{2}", re.I)

# Row prefix on a table line: serial number, then the "*" adjustment marker.
# The marker is dropped rather than stored — the description already spells it
# out ("... (ADJUSTMENT) (May 2026)"), so keeping both would be redundant.
_ROW_PREFIX_RE = re.compile(r"^\d{1,3}\s+(?:\*\s*)?|^\*\s*")

# Labels mapped to the two convenience columns, MOST SPECIFIC FIRST — the hints
# are tried in order and the first one to match any line wins.
#
# Order matters more than it looks. A payslip may print BOTH "Gross Pay"
# (basic + monthly variable component only) and "Total Gross Pay" (everything,
# including back-dated adjustments). Only the latter reconciles:
#     Total Gross Pay - Total Deductions = Net Pay
# Scanning line-by-line for any hint would hit the smaller "Gross Pay" first
# and understate the month by the whole adjustment.
_NET_HINTS = ("net pay", "nett pay", "net salary", "take home", "net amount",
              "total net")
_GROSS_HINTS = ("total gross pay", "total gross", "total earnings",
                "gross pay", "gross salary", "gross amount")
# Year-to-date restatements ("Total Gross Pay for Year 2026") repeat the same
# wording for a different number, so they are excluded from the hint search.
_YTD_RE = re.compile(r"\byear\b|\bytd\b", re.I)


def _cents(amount: str) -> int:
    return int(round(float(amount.replace(",", "")) * 100))


def decrypt_pdf(data: bytes, password: str) -> bytes:
    """Return `data` with its password removed, so the archived copy opens
    without a prompt. Raises on a wrong password or an unreadable file --
    the caller archives the original bytes and warns rather than dropping it.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        # decrypt() returns 0 on failure rather than raising.
        if not reader.decrypt(password):
            raise ValueError("payslip PDF password rejected")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def extract_text(data: bytes, password: str = "") -> str:
    """Full text of a (possibly encrypted) PDF, pages joined by newlines.

    Repeated pages are dropped. A payslip printed twice into one file (which
    the provider.s browser print flow does produce) would otherwise yield every
    line item twice and double the month's totals.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(password):
        raise ValueError("payslip PDF password rejected")
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _dedupe_key(period, text: str):
    """Identity of a payslip: its month plus the figures on it.

    Keyed on amounts rather than raw text because two renderings of the same
    page differ in logo noise once OCR is involved, while the figures do not.

    The PERIOD is part of the key, and that is the whole point: two different
    months can legitimately carry identical figures — a steady salary with no
    adjustments is exactly that — so keying on amounts alone would silently
    discard a real month as a duplicate.
    """
    return period, tuple(re.findall(r"[\d,]+\.\d{2}", text))


def page_period(text: str) -> str | None:
    """'YYYY-MM' if this page opens a payslip, else None.

    A payslip's first page carries a standalone "June 2026" heading; its
    continuation pages carry none. That is what marks the boundary when one
    file holds several months.
    """
    for line in text.splitlines():
        m = _PERIOD_HEADING_RE.match(line.strip())
        if m and (ym := _ym(m.group(1), m.group(2))):
            return ym
    m = _PERIOD_RES[0].search(text)   # "Payment Date : 10 Jun 2026"
    return _ym(m.group(1), m.group(2)) if m else None


def split_payslips(pages: list[str]) -> list[tuple[str | None, list[int]]]:
    """Group page indices into one entry per payslip.

    A single "payslip request" reply can carry SEVERAL months in one PDF —
    the sample that drove this design held June and July 2026 back to back.
    Treating the file as one document would merge two months' line items into
    a single total, so each month is split out and stored under its own
    period.

    Pages before any month heading (a cover page) attach to the first payslip.
    """
    groups: list[tuple[str | None, list[int]]] = []
    for i, text in enumerate(pages):
        period = page_period(text)
        if period is not None and (not groups or groups[-1][0] != period):
            groups.append((period, [i]))
        elif groups:
            groups[-1][1].append(i)
        else:
            groups.append((None, [i]))
    return groups


def select_pages(data: bytes, indices: list[int]) -> bytes:
    """A new PDF containing only `indices`, so each month is archived with its
    own pages rather than every month getting a copy of the whole file."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    for i in indices:
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def ocr_pages(data: bytes, scale: int = 3, psm: int = 6) -> list[str]:
    """Render each page to a bitmap and OCR it. Requires tesseract.

    Needed because the payslip was printed through a GDI driver, which
    converts all text to vector outlines: the PDF looks like text to a human
    and is completely empty to every text extractor.

    `scale` is relative to 72dpi, so 3 is ~216dpi. Measured against a real
    payslip on 2026-08-06: scale 2 and 3 read all 24 amounts correctly at
    psm 3/4/6, while scale 4 with psm 6 silently DROPPED one. Higher
    resolution is not automatically better -- do not raise this without
    re-measuring against a known-good payslip.
    """
    import pypdfium2 as pdfium
    import pytesseract

    doc = pdfium.PdfDocument(io.BytesIO(data))
    try:
        return [pytesseract.image_to_string(
            doc[i].render(scale=scale).to_pil().convert("L"),
            config=f"--psm {psm}") for i in range(len(doc))]
    finally:
        doc.close()


def read_pages(data: bytes) -> tuple[list[str], str]:
    """(one text per page, source) where source is 'pdf' or 'ocr'.

    Prefers the embedded text layer — it is exact — and falls back to OCR only
    when there is none. Callers use `source` to decide how much to trust the
    figures: OCR output gets the reconciliation check surfaced to the user.
    """
    from pypdf import PdfReader

    pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
    if any(p.strip() for p in pages):
        return pages, "pdf"
    return ocr_pages(data), "ocr"


def read_text(data: bytes, password: str = "") -> tuple[str, str]:
    """(text, source) for a whole payslip PDF — every page joined."""
    decrypted = decrypt_pdf(data, password) if password else data
    pages, source = read_pages(decrypted)
    return "\n".join(pages[i] for i in dedupe_indices(pages)), source


def has_text_layer(data: bytes, password: str = "") -> bool:
    """True if the PDF carries real text.

    A payslip printed through a GDI print driver comes out as vector outlines
    with no text at all — extract_text() then returns empty and every parser
    below silently produces nothing. Callers check this to tell "no text to
    read" apart from "text read, nothing matched", which need different advice.
    """
    try:
        return bool(extract_text(data, password).strip())
    except Exception:
        return False


def _month_num(token: str) -> int | None:
    token = token.lower()[:3]
    return _MONTHS.index(token) + 1 if token in _MONTHS else None


def _ym(raw_month: str, year: str) -> str | None:
    month = int(raw_month) if raw_month.isdigit() else _month_num(raw_month)
    if month and 1 <= month <= 12:
        return f"{int(year):04d}-{month:02d}"
    return None


def parse_period(text: str, fallback: str) -> tuple[str, str]:
    """('YYYY-MM', source) for the pay month, where source is 'document' or
    'fallback'.

    The caller MUST surface a 'fallback' result rather than filing it silently.
    `fallback` is the email's own month, and a payslip routinely covers the
    month BEFORE the one it is sent in -- a June payslip mailed on 31 July
    would be filed as July. That is a wrong month, not an approximate one, so
    it is reported instead of assumed correct.
    """
    for line in text.splitlines():
        m = _PERIOD_HEADING_RE.match(line.strip())
        if m and (ym := _ym(m.group(1), m.group(2))):
            return ym, "document"
    for pat in _PERIOD_RES:
        m = pat.search(text)
        if m and (ym := _ym(m.group(1), m.group(2))):
            return ym, "document"
    return fallback, "fallback"


def _clean_label(label: str) -> str:
    # "=" included: the year-to-date rows print "... (up to current month) ="
    # with the figure in a separate column.
    return re.sub(r"[\s.:_=-]+$", "", label).strip()


def parse_items(text: str) -> list[dict]:
    """Every '<label> <amount>' line as {section, label, amount_cents, ordinal}.

    Lines are walked in document order and `ordinal` preserves it, so the
    dashboard can render the payslip the way it was printed rather than
    re-sorting into an order the employer did not choose.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    # Tables are recognised by their column-header row; when the document has
    # none (a payslip from some other employer), fall back to the loose
    # "short line, no figure, no colon" heading rule.
    structured = any(_COLHDR_RE.match(ln) for ln in lines)

    items: list[dict] = []
    section = ""
    pending = ""        # last plain line — the heading a column header confirms
    last_was_summary = False

    def emit(label, amount_cents, sect):
        label = _clean_label(label)
        if not label:
            return
        items.append({"section": sect, "label": label,
                      "amount_cents": amount_cents, "ordinal": len(items)})

    for line in lines:
        if not line:
            continue
        # Checked BEFORE the noise filter: the column-header row is also a
        # noise line, and skipping it there would mean no section is ever
        # confirmed and every row lands under "".
        if structured and _COLHDR_RE.match(line):
            section, pending = _clean_label(pending), ""
            last_was_summary = False
            continue
        if _NOISE_RE.match(line) or _PAGE_CHROME_RE.search(line):
            last_was_summary = False  # never continue a label across chrome
            continue

        # Summary block: possibly several "label : $ amount" pairs on one line.
        pairs = _PAIR_RE.findall(line) if ":" in line else []
        if pairs and (_SUMMARY_RE.match(line) or len(pairs) > 1):
            for label, amount in pairs:
                emit(label, _cents(amount), _SUMMARY_SECTION)
            last_was_summary = True
            continue

        m = _ROW_RE.match(line)
        if m:
            amount = _cents(m.group(2))
            if line.lstrip().startswith("-") or (m.group(3) or "").upper() == "DR":
                amount = -amount
            # Year-to-date rows sit after the last table with no header of
            # their own, so without this they inherit "Deductions" and read as
            # if the year's gross pay had been deducted.
            emit(m.group(1), amount,
                 _YTD_SECTION if _YTD_RE.search(line) else section)
            last_was_summary = False
            continue

        # No figure on this line.
        if items and (line.startswith("(") or last_was_summary):
            # A wrapped description: "(May 2026)" under its row, or
            # "Contribution" under "Medisave :". Belongs to the row above, and
            # letting it stand alone would make it a bogus section heading.
            items[-1]["label"] = _clean_label(items[-1]["label"] + " " + line)
            continue
        pending = line
        if not structured and len(line.split()) <= _HEADING_MAX_WORDS \
                and ":" not in line:
            section = _clean_label(line)
    return items


def _match_hint(items: list[dict], hints: tuple[str, ...]) -> int | None:
    """Amount for the FIRST HINT that matches any item — hint order is the
    priority, not document order (see the note on _GROSS_HINTS)."""
    normalized = []
    for item in items:
        if _YTD_RE.search(item["label"]):
            continue  # year-to-date restatement, not this month's figure
        label = re.sub(r"[^a-z ]", " ", item["label"].lower())
        normalized.append((re.sub(r"\s+", " ", label).strip(), item))
    for hint in hints:
        for label, item in normalized:
            if hint in label:
                return item["amount_cents"]
    return None


def summarize(items: list[dict]) -> dict:
    """{net_cents, gross_cents} best-effort from item labels; either may be None.

    Derived, never authoritative -- `parse_items` output is the record. A
    payslip whose wording matches neither hint list still archives and renders
    in full; only the trend columns stay empty.
    """
    return {
        "net_cents": _match_hint(items, _NET_HINTS),
        "gross_cents": _match_hint(items, _GROSS_HINTS),
    }


def deductions_total(items) -> int | None:
    """The Deductions table's own Total row, or None.

    Found by SECTION, not by label: the payslip labels that row just "Total"
    — the same word the earnings table uses — so matching on wording alone
    picks whichever came first.
    """
    for item in items:
        if re.search(r"deduction", item["section"], re.I) and \
                re.match(r"total\b", item["label"].strip(), re.I):
            return abs(item["amount_cents"])
    return None


def reconciles(net_cents, gross_cents, items) -> bool | None:
    """True/False if gross - deductions == net can be checked, else None.

    The payslip states its own totals, so the figures can be checked against
    each other instead of trusted. Worth running on any extraction that could
    misread a digit — a single wrong digit in an amount breaks the identity.
    """
    if net_cents is None or gross_cents is None:
        return None
    deductions = deductions_total(items)
    if deductions is None:
        return None
    return gross_cents - deductions == net_cents


def parse_pdf(data: bytes, password: str, fallback_period: str) -> list[dict]:
    """Decrypt + parse, returning ONE ENTRY PER PAYSLIP found in the file.

    A list, not a single dict, because one "payslip request" reply can carry
    several months in one PDF — the sample this was built against held June and
    July 2026 back to back, and merging them would have produced one month with
    both sets of line items and a nonsense total. Each entry gets its own pages
    cut from the original, so a month's archived file holds only that month.

    Each entry: {period, period_source, text_source, raw_text, items,
    net_cents, gross_cents, reconciles, pdf}. Raises on decrypt failure so the
    caller can archive the original bytes and warn.
    """
    decrypted = decrypt_pdf(data, password)
    pages, text_source = read_pages(decrypted)

    out, seen = [], set()
    for period, positions in split_payslips(pages):
        text = "\n".join(pages[p] for p in positions)
        # Dedup at PAYSLIP level, not page level: the same payslip printed
        # twice into one file must collapse, but two months that happen to
        # carry identical figures must not.
        key = _dedupe_key(period, text)
        if key in seen:
            continue
        seen.add(key)
        # split_payslips only recognises a payslip's OPENING page (a standalone
        # month heading or a Payment Date). When it finds neither, fall back to
        # the full pattern set over the group's whole text before giving up on
        # the email's month — those patterns cover wordings like "For the month
        # of July 2026" that never appear as a heading.
        period_source = "document"
        if period is None:
            period, period_source = parse_period(text, fallback_period)
        items = parse_items(text)
        totals = summarize(items)
        out.append({
            "period": period,
            "period_source": period_source,
            "text_source": text_source,
            "raw_text": text,
            "items": items,
            "pdf": select_pages(decrypted, positions),
            "reconciles": reconciles(totals["net_cents"],
                                     totals["gross_cents"], items),
            **totals,
        })
    return out
