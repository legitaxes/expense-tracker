"""Transaction-email ingestion: Trust Bank, Grab e-receipts, DBS PayLah!.

Pure parsing + blocking IMAP fetch. No Telegram/DB deps here so the parsers
are unit-testable against real bodies. The bot wires this into its scheduler
and does the DB insert + notify.

All senders mail HTML-only; we strip tags, collapse whitespace, then regex.
Shapes (from real emails):

Trust (from_us@trustbank.sg):
  local card : You've spent SGD 3.39 at BUS/MRT SG on 9 Jul 2026 06:24SGT with Trust Link card.
  overseas   : You've spent SGD 82.69 at Steam ... with Trust Link card on 6 Jul 2026 22:11SGT.
  paynow out : Your PayNow transfer of SGD 7.00 from A/C ending 1234 to NAME on 09 Jul 2026 14:56 SGT is successful.
  cancelled  : We've cancelled your purchase of SGD 18.50 at Grab* A-XXXX ... on 12 Jun 2026 22:20SGT ...

Grab (no-reply@grab.com, subject "Your Grab E-Receipt"):
  food/mart  : TOTAL SGD 1.40 DATE | TIME 10 Jul 26 12:18 +0800 ... Vehicle type: GrabFood ... Order from: Coke&Go Cooler 123456789 - <address> Profile: ...
  ride       : Total Paid SGD 17.70 ... Picked up on 12 June 2026 ...

PayLah (paylah.alert@dbs.com, subject "Transaction Alerts") — one table for all
of Scan & Pay / Transfer / Bill Payment (note: no YEAR in the body):
  Date & Time: 03 Jul 13:40 (SGT)  Amount: SGD1.60
  From: PayLah! Wallet (Mobile ending 1111)  To: KOPITIAM KAYA TOAST

FairPrice Group app (no-reply@fairprice.com.sg, subject "...app receipt"):
  "Summary Total $6.70 Paid By $6.70 Apple Pay (**** 1234)" — no store, no time.

ShopBack Pay (hello@info.shopback.sg, subject "Your ShopBack Pay receipt - $X"):
  "payment you made at <merchant> using ShopBack Pay ... Payment date
   27 Jun 2026, 8:38 PM ... Total paid $10.80" (same sender also mails
   cashback notices — subject-gated).

Dedup policy: spends paid through an app AND charged to the Trust card fire
BOTH a merchant receipt and a Trust alert. The merchant receipt is
authoritative (real merchant name, final total — Trust sees pre-auth holds
that later get cancelled), so Trust alerts matching _TRUST_DEDUP_RE are
skipped: "Grab*...", "ShopBack ...", "...FairPrice App Paymen..." (app
payments only; direct card taps at FairPrice tills still import from Trust).

Unknown senders go through `suggest_receipt` — a conservative heuristic that
asks the user to confirm via Telegram buttons instead of auto-importing.
"""
from __future__ import annotations
import email
import email.header
import email.utils
import html
import imaplib
import re
from datetime import datetime, timedelta, timezone

TRUST_SENDER = "from_us@trustbank.sg"
GRAB_SENDER = "no-reply@grab.com"
PAYLAH_SENDER = "paylah.alert@dbs.com"
FAIRPRICE_SENDER = "no-reply@fairprice.com.sg"
SHOPBACK_SENDER = "hello@info.shopback.sg"
DBS_SENDER = "ibanking.alert@dbs.com"

# Telegram label + DB `source` value per sender.
SOURCES = {
    TRUST_SENDER: ("Trust", "trust-email"),
    GRAB_SENDER: ("Grab", "grab-email"),
    PAYLAH_SENDER: ("PayLah!", "paylah-email"),
    FAIRPRICE_SENDER: ("FairPrice", "fairprice-email"),
    SHOPBACK_SENDER: ("ShopBack", "shopback-email"),
    DBS_SENDER: ("DBS", "dbs-email"),
}

IMAP_HOST = "imap.gmail.com"
_SGT = timezone(timedelta(hours=8))

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# (pattern, kind). Order matters: local must be tried before overseas — the local
# text also contains " with Trust ... card", so a looser overseas pattern could
# mis-capture it. Local is pinned by "<time>SGT with Trust" (date BEFORE card).
_PATTERNS = [
    (re.compile(
        r"You've spent SGD ([\d,]+\.\d{2}) at (.+?) on "
        r"(\d{1,2} \w{3} \d{4}) (\d{1,2}:\d{2})\s?SGT with Trust"), "card"),
    (re.compile(
        r"You've spent SGD ([\d,]+\.\d{2}) at (.+?) with Trust .*? card on "
        r"(\d{1,2} \w{3} \d{4}) (\d{1,2}:\d{2})\s?SGT"), "card"),
    (re.compile(
        r"PayNow transfer of SGD ([\d,]+\.\d{2}) from A/C ending \d+ to (.+?) on "
        r"(\d{1,2} \w{3} \d{4}) (\d{1,2}:\d{2})\s?SGT is successful"), "paynow"),
]

_TRUST_CANCELLED_RE = re.compile(
    r"cancelled your purchase of SGD ([\d,]+\.\d{2}) at (.+?) on "
    r"\d{1,2} \w{3} \d{4}")

_GRAB_FOOD_RE = re.compile(
    r"TOTAL SGD ([\d,]+\.\d{2}) DATE \| TIME (\d{1,2} \w{3} \d{2}) (\d{2}:\d{2})")
_GRAB_VEHICLE_RE = re.compile(r"Vehicle type: (\w+)")
# Merchant runs until the numeric store ID; fall back to the next fixed label.
_GRAB_MERCHANT_RES = (re.compile(r"Order from: (.+?) \d{5,}"),
                      re.compile(r"Order from: (.+?) Profile:"))
_GRAB_RIDE_RE = re.compile(r"Total Paid SGD ([\d,]+\.\d{2})")

_PAYLAH_AMOUNT_RE = re.compile(r"Amount: SGD([\d,]+\.\d{2})")
_PAYLAH_DT_RE = re.compile(r"Date & Time: (\d{1,2} \w{3}) (\d{1,2}:\d{2}) \(SGT\)")
_PAYLAH_TO_RE = re.compile(r"To: (.+?) To view your transactions")
# DBS iBanking PAYNOW uses the same Date&Time/Amount table; only the To
# terminator and the recipient-suffix style ("UEN ending") differ.
_DBS_TO_RE = re.compile(r"To: (.+?) If unauthorised")
_ENDING_RE = re.compile(r"\s*\((?:Mobile|UEN) ending [^)]*\)$")

# "Summary Total $6.70 Paid By $6.70 ..." — anchor on "Summary Total" so the
# "Total savings" row can't be mistaken for the amount.
_FP_TOTAL_RE = re.compile(r"Summary Total \$([\d,]+\.\d{2})")

_SB_MERCHANT_RE = re.compile(r"payment you made at (.+?) using ShopBack Pay")
_SB_TOTAL_RE = re.compile(r"Total paid \$([\d,]+\.\d{2})")
_SB_DATE_RE = re.compile(r"Payment date (\d{1,2} \w{3} \d{4}, \d{1,2}:\d{2} [AP]M)")

# Trust alerts to drop because a richer receipt email covers the same spend:
# Grab* (pre-auth noise), ShopBack <merchant>, NTUC FairPrice App Paymen[t]
# (app payments only — a direct card tap at a FairPrice till still imports).
_TRUST_DEDUP_RE = re.compile(r"^(grab\*|shopback\b)|fairprice app paymen", re.I)

# Generic catch-all for senders we have no parser for: receipt-ish subject +
# a Total-style dollar amount → suggest to the user instead of auto-importing.
_SUGGEST_SUBJECT_RE = re.compile(
    r"receipt|invoice|order confirm|payment confirm|payment received", re.I)
_SUGGEST_TOTAL_RE = re.compile(
    r"(?:grand total|total paid|total amount|amount paid|total(?: \(incl[^)]*\))?)"
    r"[: ]*\$?\s?(?:SGD\s?)?\$?([\d,]+\.\d{2})", re.I)

# Incoming-money heuristic for senders we already trust. Deliberately loose —
# it only ever *suggests* (user confirms via button); a miss = today's
# behaviour (silent skip), never a wrong auto-import.
_RECEIVED_RE = re.compile(r"\breceived\b|\bcredited\b", re.I)
_ANY_SGD_RE = re.compile(r"SGD\s?([\d,]+\.\d{2})")


def _to_text(html_body: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", html_body))).strip()


def _cents(amount: str) -> int:
    return int(round(float(amount.replace(",", "")) * 100))


def parse_trust_email(subject: str, html_body: str) -> dict | None:
    """Return {amount_cents, note, created_at, kind} for a spend/transfer email,
    or None if the body matches no known transaction shape."""
    text = _to_text(html_body)
    for pat, kind in _PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        amount, name, d, t = m.groups()
        dt = datetime.strptime(f"{d} {t}", "%d %b %Y %H:%M")
        return {
            "amount_cents": _cents(amount),
            "note": name.strip(),
            "created_at": dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind,
        }
    return None


def parse_trust_cancellation(html_body: str) -> dict | None:
    """{amount_cents, note} for a "Transaction cancelled" alert (money returned),
    e.g. a merchant pre-auth hold being released."""
    m = _TRUST_CANCELLED_RE.search(_to_text(html_body))
    if not m:
        return None
    return {"amount_cents": _cents(m.group(1)), "note": m.group(2).strip()}


def parse_grab_email(subject: str, html_body: str, msg_dt: datetime) -> dict | None:
    """Grab e-receipt → transaction. Food/mart orders carry a full timestamp and
    merchant; rides only a pickup date, so they use the email's own time
    (receipts arrive at ride end)."""
    if "Grab E-Receipt" not in subject:
        return None  # marketing/announcement mail from the same sender
    text = _to_text(html_body)
    m = _GRAB_FOOD_RE.search(text)
    if m:
        amount, d, t = m.groups()
        vehicle = _GRAB_VEHICLE_RE.search(text)
        label = vehicle.group(1) if vehicle else "Grab"
        merchant = next((mm.group(1).strip() for pat in _GRAB_MERCHANT_RES
                         if (mm := pat.search(text))), "")
        dt = datetime.strptime(f"{d} {t}", "%d %b %y %H:%M")
        return {
            "amount_cents": _cents(amount),
            "note": f"{label} {merchant}".strip(),
            "created_at": dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "grab",
        }
    m = _GRAB_RIDE_RE.search(text)
    if m:
        return {
            "amount_cents": _cents(m.group(1)),
            "note": "Grab ride",
            "created_at": msg_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "grab",
        }
    return None


def parse_paylah_email(subject: str, html_body: str, msg_dt: datetime) -> dict | None:
    """PayLah! alert → transaction. One shape covers Scan & Pay / Transfer /
    Bill Payment. Only outgoing wallet spends (From: PayLah! Wallet) count."""
    text = _to_text(html_body)
    if "From: PayLah! Wallet" not in text:
        return None  # top-up / incoming money / non-transaction notice
    ma = _PAYLAH_AMOUNT_RE.search(text)
    md = _PAYLAH_DT_RE.search(text)
    mto = _PAYLAH_TO_RE.search(text)
    if not (ma and md and mto):
        return None
    day_month, hm = md.groups()
    # The body has no year. Assume the email's year; if that lands in the
    # future (early-Jan alert for a late-Dec txn), roll back one year.
    dt = datetime.strptime(f"{day_month} {msg_dt.year} {hm}", "%d %b %Y %H:%M")
    if dt > msg_dt + timedelta(days=2):
        dt = dt.replace(year=dt.year - 1)
    return {
        "amount_cents": _cents(ma.group(1)),
        "note": _ENDING_RE.sub("", mto.group(1).strip()),
        "created_at": dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": "paylah",
    }


def parse_dbs_email(subject: str, html_body: str, msg_dt: datetime) -> dict | None:
    """DBS iBanking alert → transaction. Only outgoing PayNow from the bank
    account ("We refer to your PAYNOW"); incoming "You've received a transfer"
    alerts are ignored. Same no-year table as PayLah."""
    text = _to_text(html_body)
    if "We refer to your PAYNOW" not in text:
        return None
    ma = _PAYLAH_AMOUNT_RE.search(text)
    md = _PAYLAH_DT_RE.search(text)
    mto = _DBS_TO_RE.search(text)
    if not (ma and md and mto):
        return None
    day_month, hm = md.groups()
    dt = datetime.strptime(f"{day_month} {msg_dt.year} {hm}", "%d %b %Y %H:%M")
    if dt > msg_dt + timedelta(days=2):
        dt = dt.replace(year=dt.year - 1)
    return {
        "amount_cents": _cents(ma.group(1)),
        "note": _ENDING_RE.sub("", mto.group(1).strip()),
        "created_at": dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": "dbs-paynow",
    }


def parse_fairprice_email(subject: str, html_body: str, msg_dt: datetime) -> dict | None:
    """FairPrice Group app receipt → transaction. The body has no store name and
    no timestamp — note is just "FairPrice" (→ Groceries) and the email's own
    time is used (receipts arrive at checkout)."""
    if "app receipt" not in subject.lower():
        return None
    m = _FP_TOTAL_RE.search(_to_text(html_body))
    if not m:
        return None
    return {
        "amount_cents": _cents(m.group(1)),
        "note": "FairPrice",
        "created_at": msg_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": "fairprice",
    }


def parse_shopback_email(subject: str, html_body: str, msg_dt: datetime) -> dict | None:
    """ShopBack Pay receipt → transaction. Same sender also mails cashback
    notices and promos — only "ShopBack Pay receipt" subjects count."""
    if "ShopBack Pay receipt" not in subject:
        return None
    text = _to_text(html_body)
    mt = _SB_TOTAL_RE.search(text)
    if not mt:
        return None
    mm = _SB_MERCHANT_RE.search(text)
    md = _SB_DATE_RE.search(text)
    dt = (datetime.strptime(md.group(1), "%d %b %Y, %I:%M %p") if md else msg_dt)
    return {
        "amount_cents": _cents(mt.group(1)),
        "note": mm.group(1).strip() if mm else "ShopBack Pay",
        "created_at": dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": "shopback",
    }


def suggest_receipt(sender: str, subject: str, html_body: str) -> dict | None:
    """Heuristic for receipts from senders we have NO parser for: receipt-ish
    subject + a Total-style amount in the body. The caller asks the user to
    confirm instead of auto-importing. Returns {amount_cents, note} or None."""
    if not _SUGGEST_SUBJECT_RE.search(subject):
        return None
    m = _SUGGEST_TOTAL_RE.search(_to_text(html_body))
    if not m:
        return None
    return {"amount_cents": _cents(m.group(1)), "note": subject.strip()[:80]}


def suggest_income(sender: str, subject: str, html_body: str) -> dict | None:
    """Incoming-money alert from a known bank sender → suggest as income.
    Returns {amount_cents, note} or None."""
    if sender not in (PAYLAH_SENDER, DBS_SENDER, TRUST_SENDER):
        return None
    text = _to_text(html_body)
    if not _RECEIVED_RE.search(text):
        return None
    m = _ANY_SGD_RE.search(text)
    if not m:
        return None
    return {"amount_cents": _cents(m.group(1)), "note": subject.strip()[:80]}


def is_spend_email(html_body: str) -> bool:
    """True if this IS a Trust spend/transfer alert (so a parse=None means our
    regex broke on a real transaction and the caller should surface it)."""
    text = _to_text(html_body)
    return "You've spent" in text or "PayNow transfer of" in text


def classify_email(sender: str, subject: str, html_body: str,
                   msg_dt: datetime) -> tuple[str, dict | None]:
    """Route one fetched email. Returns (action, payload):
      ("import", parsed)  — insert as a transaction
      ("cancelled", info) — Trust refunded a spend; warn, don't insert
      ("suggest", info)   — unknown sender, looks like a receipt; ask the user
      ("suggest_income", info) — known bank sender, looks like incoming money;
                                  ask the user (never auto-imported)
      ("unparsed", None)  — looks like a real transaction but no pattern matched
      ("skip", None)      — not a transaction (or a deduped Trust alert)
    """
    if sender == GRAB_SENDER:
        parsed = parse_grab_email(subject, html_body, msg_dt)
        if parsed:
            return "import", parsed
        return ("unparsed", None) if "Grab E-Receipt" in subject else ("skip", None)
    if sender == PAYLAH_SENDER:
        parsed = parse_paylah_email(subject, html_body, msg_dt)
        if parsed:
            return "import", parsed
        if "From: PayLah! Wallet" in _to_text(html_body):
            return "unparsed", None
        info = suggest_income(sender, subject, html_body)
        return ("suggest_income", info) if info else ("skip", None)
    if sender == FAIRPRICE_SENDER:
        parsed = parse_fairprice_email(subject, html_body, msg_dt)
        if parsed:
            return "import", parsed
        return ("unparsed", None) if "app receipt" in subject.lower() else ("skip", None)
    if sender == SHOPBACK_SENDER:
        parsed = parse_shopback_email(subject, html_body, msg_dt)
        if parsed:
            return "import", parsed
        return ("unparsed", None) if "ShopBack Pay receipt" in subject else ("skip", None)
    if sender == DBS_SENDER:
        parsed = parse_dbs_email(subject, html_body, msg_dt)
        if parsed:
            return "import", parsed
        if "We refer to your PAYNOW" in _to_text(html_body):
            return "unparsed", None
        info = suggest_income(sender, subject, html_body)
        return ("suggest_income", info) if info else ("skip", None)
    if sender == TRUST_SENDER:
        parsed = parse_trust_email(subject, html_body)
        if parsed:
            if _TRUST_DEDUP_RE.search(parsed["note"]):
                return "skip", None  # the merchant's own receipt email wins
            return "import", parsed
        info = parse_trust_cancellation(html_body)
        if info:
            if _TRUST_DEDUP_RE.search(info["note"]):
                return "skip", None  # pre-auth hold release; never imported anyway
            return "cancelled", info
        if is_spend_email(html_body):
            return "unparsed", None
        info = suggest_income(sender, subject, html_body)
        return ("suggest_income", info) if info else ("skip", None)
    # Unknown sender: maybe a receipt from a service we don't parse yet.
    info = suggest_receipt(sender, subject, html_body)
    return ("suggest", info) if info else ("skip", None)


# ---- IMAP ---------------------------------------------------------------
# The poll fetches ALL new inbox mail (not just known senders) so the generic
# receipt catch-all can see receipts from services we don't parse yet. Volume
# is a handful of mails per 15-min tick; non-receipts are dropped after one
# regex pass.


def _extract(msg) -> tuple[str, str, str, datetime]:
    sender = email.utils.parseaddr(msg["From"] or "")[1].lower()
    subject = str(email.header.make_header(email.header.decode_header(msg["Subject"] or "")))
    body = ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/html":
            body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8",
                                                         "replace")
            break
    try:
        msg_dt = email.utils.parsedate_to_datetime(msg["Date"]) \
            .astimezone(_SGT).replace(tzinfo=None)
    except (TypeError, ValueError):
        msg_dt = datetime.now()
    return sender, subject, body, msg_dt


def _login(host, user, password):
    imap = imaplib.IMAP4_SSL(host)
    imap.login(user, password)
    return imap


def seed_max_uid(host: str, user: str, password: str) -> tuple[int, int]:
    """First-run: return (uidvalidity, current high-water UID) WITHOUT fetching
    bodies, so go-forward ingestion starts from 'now' and skips all history."""
    imap = _login(host, user, password)
    try:
        _, [meta] = imap.select("INBOX", readonly=True)
        validity = int(dict(re.findall(rb"(\w+) (\S+)", meta)).get(b"UIDVALIDITY", b"0"))
        # UIDNEXT (cached from the SELECT response) avoids enumerating the
        # whole mailbox; UIDNEXT-1 is the highest UID assigned so far.
        _, data = imap.response("UIDNEXT")
        if data and data[0]:
            return validity, int(data[0]) - 1
        _, [data] = imap.uid("search", None, "ALL")
        uids = [int(x) for x in data.split()]
        return validity, (max(uids) if uids else 0)
    finally:
        imap.logout()


def fetch_since(host: str, user: str, password: str, last_uid: int):
    """Return (uidvalidity, [(uid, sender, subject, html, msg_dt), ...]) for
    ALL inbox emails with UID > last_uid, sorted ascending. Bodies fetched
    read-only (inbox unchanged)."""
    imap = _login(host, user, password)
    try:
        _, [meta] = imap.select("INBOX", readonly=True)
        validity = int(dict(re.findall(rb"(\w+) (\S+)", meta)).get(b"UIDVALIDITY", b"0"))
        _, [data] = imap.uid("search", None, f"(UID {last_uid + 1}:*)")
        # ponytail: IMAP returns the highest UID even when none actually exceed
        # last_uid (the `N:*` quirk) — so filter explicitly.
        uids = sorted(u for u in (int(x) for x in data.split()) if u > last_uid)
        out = []
        for uid in uids:
            _, fetched = imap.uid("fetch", str(uid), "(RFC822)")
            if not fetched or not fetched[0]:
                continue
            msg = email.message_from_bytes(fetched[0][1])
            sender, subject, body, msg_dt = _extract(msg)
            out.append((uid, sender, subject, body, msg_dt))
        return validity, out
    finally:
        imap.logout()


# ---- cursor (high-water UID persisted on the data volume) ---------------

def read_cursor(path: str) -> tuple[int, int] | None:
    """Return (uidvalidity, last_uid) or None if never seeded."""
    try:
        with open(path) as f:
            validity, uid = f.read().split()
        return int(validity), int(uid)
    except (FileNotFoundError, ValueError):
        return None


def write_cursor(path: str, validity: int, uid: int) -> None:
    with open(path, "w") as f:
        f.write(f"{validity} {uid}")
