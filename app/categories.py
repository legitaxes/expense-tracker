from __future__ import annotations
from app import db
from app.config import DEFAULT_CATEGORY, CATEGORIES
from app.merchants import merchant_stem
from app.trust_email import SOURCES as _EMAIL_SOURCES

# (keywords, category) — first rule with any matching substring wins.
# Keywords are matched case-insensitively against the note text.
# Common Singaporean merchant/term shorthand; near-synonyms are pruned.
# NOTE: Food must precede Transport so "grabfood" resolves to Food, not the
# "grab" (ride-hailing) rule below it.
KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    # SG supermarkets (grabmart before the "grab" ride rule, like grabfood)
    (("fairprice", "ntuc", "sheng siong", "giant", "cold storage",
      "donki", "redmart", "grabmart"), "Groceries"),
    # Hawker, kopitiam, fast food, delivery
    (("hawker", "kopitiam", "kopi", "makan", "cai png", "chicken rice",
      "mcdonald", "kfc", "mixue", "koi", "foodpanda", "grabfood",
      "restaurant", "cafe"), "Food"),
    # Ride-hailing, public transit, fuel, parking
    (("grab", "gojek", "tada", "taxi", "mrt", "bus", "ezlink", "simplygo",
      "petrol", "parking", "erp"), "Transport"),
    # Online marketplaces + retail
    (("shopee", "lazada", "taobao", "qoo10", "amazon", "uniqlo",
      "decathlon", "ikea"), "Shopping"),
    # Telco, utilities, subscriptions, insurance, rent
    (("sp group", "singtel", "starhub", "m1", "simba", "netflix", "spotify",
      "disney", "youtube", "insurance", "rent", "hdb", "town council",
      "wifi"), "Bills"),
    # Clinics, pharmacies, gym, salon
    (("clinic", "polyclinic", "dentist", "pharmacy", "guardian", "watsons",
      "unity", "hospital", "gym", "salon", "barber"), "Health"),
    # Flights, stays, booking platforms (day-to-day overseas spend is /trip mode's job)
    (("flight", "hotel", "hostel", "airport", "changi", "airbnb", "agoda",
      "klook", "trip.com", "traveloka", "scoot", "jetstar"), "Travel"),
    # Cinemas, games, concerts
    (("gv", "shaw", "cathay", "cinema", "steam", "concert"), "Entertainment"),
]


def guess_category(note: str) -> str:
    text = note.lower()
    for keywords, category in KEYWORD_RULES:
        if any(kw in text for kw in keywords):
            return category
    return DEFAULT_CATEGORY


def effective_category(conn, note: str) -> tuple[str, str]:
    """(category, origin) for a new spend. Origin is 'trip' | 'memory' | 'keyword'.

    Precedence: trip mode routes everything to Travel; a remembered merchant
    beats the keyword rules (otherwise correcting a merchant the keyword list
    already matches would do nothing); keywords are the fallback.

    Recurring-rule postings and income flows deliberately bypass this."""
    if db.get_setting(conn, "trip_mode", "0") == "1":
        return "Travel", "trip"
    stem = merchant_stem(note)
    if stem:
        remembered = db.recall_merchant(conn, stem)
        if remembered:
            return remembered, "memory"
    return guess_category(note), "keyword"


# Allowlist of sources known to carry real merchant strings — exactly the
# sources emitted by the email parsers in app.trust_email.SOURCES. Deriving it
# from that map (rather than retyping the strings) means a future parser
# added there is automatically learnable with no change here.
#
# This is deliberately an allowlist, not a denylist: dashboard/bot-typed notes
# are free-form phrases, recurring postings are user-authored labels, and the
# suggest-email catch-all's note is an email *subject*, not a merchant — none
# of those are merchant strings. An unknown future source defaults to NOT
# learning, which is the safe direction (a missed learning opportunity, not a
# polluted merchant table).
_LEARNABLE_SOURCES = frozenset(source for _label, source in _EMAIL_SOURCES.values())


def learn_from_correction(conn, row, category: str) -> str | None:
    """Teach merchant memory from a user's category correction.

    Returns the stem that was learned, or None if the row does not qualify:
    only rows from the known email parsers (see _LEARNABLE_SOURCES) carry real
    merchant strings; income rows and categories outside CATEGORIES are not
    spend categories; and reimbursement rows (negative amounts, note prefixed
    "↩️ ") are paybacks rather than charges. `row` is any mapping with
    note/source/amount_cents/type (sqlite3.Row or plain dict)."""
    if row["source"] not in _LEARNABLE_SOURCES:
        return None
    # type may be absent on some plain-dict test rows; treat missing as
    # "expense" (sqlite3.Row has no .get(), so check membership instead).
    if "type" in row.keys() and row["type"] != "expense":
        return None
    if category not in CATEGORIES:
        return None
    # Reimbursement rows are already excluded by source (suggest-email is not
    # in _LEARNABLE_SOURCES), but keep the amount check as defence in depth.
    if row["amount_cents"] < 0:
        return None
    stem = merchant_stem(row["note"])
    if not stem:
        return None
    db.remember_merchant(conn, stem, category)
    return stem
