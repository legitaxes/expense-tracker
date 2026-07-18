from app.categories import guess_category, KEYWORD_RULES
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
