import pytest
from app import db
from app.categories import (guess_category, effective_category,
                            learn_from_correction, KEYWORD_RULES)
from app.config import CATEGORIES, DEFAULT_CATEGORY


def test_unknown_falls_back_to_other():
    assert guess_category("qwerty zxcv") == DEFAULT_CATEGORY


def test_every_rule_targets_a_real_category():
    targets = {category for _keywords, category in KEYWORD_RULES}
    assert targets <= set(CATEGORIES)


def test_example_mappings():
    assert guess_category("grab home") == "Transport"
    assert guess_category("ntuc groceries") == "Groceries"
    assert guess_category("makan at hawker") == "Food"
    assert guess_category("netflix") == "Bills"


def test_grabfood_beats_grab():
    # Food rule precedes Transport so the "grab" substring in "grabfood"
    # does not misfire to Transport.
    assert guess_category("grabfood dinner") == "Food"


def test_case_insensitive():
    assert guess_category("GRAB to work") == "Transport"


def test_travel_bookings():
    assert guess_category("scoot to bangkok") == "Travel"
    assert guess_category("Klook tickets") == "Travel"
    assert guess_category("agoda 2 nights") == "Travel"
    assert guess_category("hotel deposit") == "Travel"


def test_entertainment_keeps_fun():
    # travel keywords moved out of Entertainment; cinemas/games stayed
    assert guess_category("gv movie night") == "Entertainment"
    assert guess_category("steam sale") == "Entertainment"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


def test_keyword_is_the_baseline(conn):
    assert effective_category(conn, "kopi at hawker") == ("Food", "keyword")


def test_memory_beats_keyword(conn):
    # Without memory, "shopee sg" is Shopping by keyword.
    assert effective_category(conn, "SHOPEE SG") == ("Shopping", "keyword")
    db.remember_merchant(conn, "shopee", "Bills")
    assert effective_category(conn, "SHOPEE SG") == ("Bills", "memory")


def test_memory_generalises_across_outlets(conn):
    db.remember_merchant(conn, "ntuc fp", "Groceries")
    assert effective_category(
        conn, "NTUC FP-MARINA BAY SINGAPORE SG") == ("Groceries", "memory")


def test_trip_mode_beats_memory(conn):
    db.remember_merchant(conn, "shopee", "Bills")
    db.set_setting(conn, "trip_mode", "1")
    assert effective_category(conn, "SHOPEE SG") == ("Travel", "trip")


def test_unrememberable_note_falls_through_to_keywords(conn):
    # merchant_stem returns None here; that must not raise or short-circuit.
    assert effective_category(conn, "12345") == ("Other", "keyword")


def _row(note, source, amount_cents=1000, type_="expense"):
    return {"note": note, "source": source, "amount_cents": amount_cents, "type": type_}


def test_email_correction_teaches(conn):
    # trust-email is one of the known email-parser sources (allowlisted),
    # so a correction on it is a real merchant string and should teach.
    stem = learn_from_correction(conn, _row("SHOPEE SG", "trust-email"), "Bills")
    assert stem == "shopee"
    assert db.recall_merchant(conn, "shopee") == "Bills"


def test_manual_note_teaches_nothing(conn):
    # Typed notes are free-form phrases, not merchants — they would pollute
    # the merchant table that /merchants exists to make readable.
    assert learn_from_correction(conn, _row("lunch canteen", "telegram"), "Food") is None
    assert db.list_merchants(conn) == []


def test_recurring_posting_teaches_nothing(conn):
    assert learn_from_correction(conn, _row("bus fare", "recurring"), "Transport") is None
    assert db.list_merchants(conn) == []


def test_reimbursement_row_teaches_nothing(conn):
    # Reimbursements carry source 'suggest-email' and a note of "↩️ " + the
    # original, so only the negative amount distinguishes them. (suggest-email
    # is also excluded by the allowlist now, but the amount guard stays as
    # defence in depth.)
    row = _row("↩️ SHOPEE SG", "suggest-email", amount_cents=-1000)
    assert learn_from_correction(conn, row, "Bills") is None
    assert db.list_merchants(conn) == []


def test_unstemmable_note_teaches_nothing(conn):
    assert learn_from_correction(conn, _row("12345", "trust-email"), "Other") is None
    assert db.list_merchants(conn) == []


def test_income_row_teaches_nothing(conn):
    # FINDING 2 regression: an income row must never teach "Income" as a
    # merchant category — "Income" is not in app.config.CATEGORIES and no
    # bot menu or web picker can represent it.
    row = _row("Your PayNow transfer is complete", "trust-email", type_="income")
    assert learn_from_correction(conn, row, "Income") is None
    assert db.list_merchants(conn) == []


def test_dashboard_row_teaches_nothing(conn):
    # FINDING 3 regression: web Add-sheet rows are free-form typed phrases
    # ("lunch with mum"), not merchant strings — must not teach.
    assert learn_from_correction(conn, _row("lunch with mum", "dashboard"), "Food") is None
    assert db.list_merchants(conn) == []


def test_suggest_email_row_teaches_nothing(conn):
    # FINDING 4 regression: the catch-all importer's note is an email subject
    # ("Your receipt from Apple"), not a merchant — must not teach, or a later
    # unrelated receipt from a different merchant sharing the subject phrasing
    # would silently inherit the wrong category.
    row = _row("Your receipt from Apple", "suggest-email")
    assert learn_from_correction(conn, row, "Shopping") is None
    assert db.list_merchants(conn) == []


def test_trust_email_row_teaches(conn):
    # Positive case: a known email-parser source still teaches.
    stem = learn_from_correction(conn, _row("KLOOK TICKETS SG", "trust-email"), "Travel")
    assert stem == "klook tickets"
    assert db.recall_merchant(conn, "klook tickets") == "Travel"
