import pytest
from app.parsing import parse_add


@pytest.mark.parametrize("text,amount_cents,note", [
    ("12.50 lunch at hawker", 1250, "lunch at hawker"),
    ("$4.50 grab home", 450, "grab home"),
    ("S$12 dinner", 1200, "dinner"),
    ("lunch 12.50", 1250, "lunch"),      # amount need not be first
    ("12", 1200, ""),                    # amount only, no note
])
def test_parse_add_extracts_amount_and_note(text, amount_cents, note):
    assert parse_add(text) == (amount_cents, note)


def test_parse_add_no_amount_returns_none():
    assert parse_add("no amount mentioned here") == (None, "no amount mentioned here")


def test_parse_add_empty():
    assert parse_add("   ") == (None, "")


from app.parsing import parse_recurring_add


def test_parse_recurring_daily():
    assert parse_recurring_add(["50", "daily", "coffee", "kopi"]) == (5000, "daily", None, "coffee kopi")


def test_parse_recurring_daily_no_note():
    assert parse_recurring_add(["5", "daily"]) == (500, "daily", None, "")


def test_parse_recurring_weekly():
    assert parse_recurring_add(["10", "weekly", "mon", "groceries"]) == (1000, "weekly", 0, "groceries")


def test_parse_recurring_weekly_case_insensitive():
    assert parse_recurring_add(["10", "weekly", "SUN", "x"]) == (1000, "weekly", 6, "x")


def test_parse_recurring_monthly():
    assert parse_recurring_add(["1200", "monthly", "1", "rent"]) == (120000, "monthly", 1, "rent")


def test_parse_recurring_monthly_day_31_ok():
    amount, freq, anchor, note = parse_recurring_add(["50", "monthly", "31", "salary"])
    assert (freq, anchor, note) == ("monthly", 31, "salary")


@pytest.mark.parametrize("args", [
    [],                               # nothing
    ["50"],                           # no frequency
    ["abc", "daily"],                 # bad amount
    ["0", "daily", "x"],              # non-positive amount
    ["10", "weekly"],                 # weekly missing day
    ["10", "weekly", "funday", "x"],  # bad weekday
    ["10", "monthly"],                # monthly missing day
    ["10", "monthly", "0", "x"],      # day below range
    ["10", "monthly", "32", "x"],     # day above range
    ["10", "yearly", "x"],            # unknown frequency
])
def test_parse_recurring_errors(args):
    with pytest.raises(ValueError):
        parse_recurring_add(args)


from app.parsing import parse_plain_text


def test_parse_plain_text_accepts():
    assert parse_plain_text("kopi 2.80") == (280, "kopi")
    assert parse_plain_text("2.80 kopi") == (280, "kopi")
    assert parse_plain_text("$15 chicken rice set") == (1500, "chicken rice set")


def test_parse_plain_text_rejects():
    assert parse_plain_text("just chatting here") == (None, "")
    assert parse_plain_text("2.80") == (None, "")            # amount, no note
    assert parse_plain_text("kopi 2.80 3.50") == (None, "")  # two amounts = ambiguous
    assert parse_plain_text("") == (None, "")
