# tests/test_money.py
import pytest
from app.money import dollars_to_cents, format_cents


@pytest.mark.parametrize("value,expected", [
    ("12", 1200),
    ("12.50", 1250),
    ("12.5", 1250),
    ("$12.50", 1250),
    ("S$12.50", 1250),
    ("1,234.50", 123450),
    (12.5, 1250),
    (0, 0),
])
def test_dollars_to_cents(value, expected):
    assert dollars_to_cents(value) == expected


def test_dollars_to_cents_rounds_half_up():
    assert dollars_to_cents("0.005") == 1


@pytest.mark.parametrize("cents,expected", [
    (1250, "S$12.50"),
    (0, "S$0.00"),
    (123450, "S$1,234.50"),
    (-1250, "-S$12.50"),      # reimbursement rows / over-reimbursed nets
    (-123450, "-S$1,234.50"),
])
def test_format_cents(cents, expected):
    assert format_cents(cents) == expected
