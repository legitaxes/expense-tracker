from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP

_PREFIXES = ("S$", "s$", "$")


def dollars_to_cents(value) -> int:
    """Convert a dollar amount to integer cents.

    Accepts numbers (12.5) or strings ('12.50', '$12.50', 'S$12.50', '1,234.50').
    """
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        for sym in _PREFIXES:
            if cleaned.startswith(sym):
                cleaned = cleaned[len(sym):]
                break
        dec = Decimal(cleaned.strip())
    else:
        dec = Decimal(str(value))
    cents = (dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def format_cents(cents: int) -> str:
    """Render integer cents as 'S$x.xx' ('-S$x.xx' when negative, e.g.
    reimbursement rows and over-reimbursed nets — not 'S$-x.xx')."""
    sign = "-" if cents < 0 else ""
    return f"{sign}S${abs(cents) / 100:,.2f}"
