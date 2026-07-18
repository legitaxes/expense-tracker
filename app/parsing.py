from __future__ import annotations
import re
from app.money import dollars_to_cents
from app.recurrence import weekday_index

# A token is "money" if it is exactly an optional S$/$ prefix + digits + optional .dd
AMOUNT_RE = re.compile(r"(?:S\$|\$)?\d+(?:\.\d{1,2})?")


def parse_add(text: str) -> tuple[int | None, str]:
    """Split '/add' text into (amount_cents, note).

    Returns (None, text) when no token looks like a money amount.
    """
    text = text.strip()
    if not text:
        return None, ""
    tokens = text.split()
    for i, tok in enumerate(tokens):
        if AMOUNT_RE.fullmatch(tok):
            note = " ".join(tokens[:i] + tokens[i + 1:]).strip()
            return dollars_to_cents(tok), note
    return None, text


def parse_recurring_add(args: list[str]) -> tuple[int, str, int | None, str]:
    """Parse tokens after '/recurring add' into (amount_cents, frequency, anchor, note).

    Grammar:
      <amount> daily <note...>
      <amount> weekly <mon|tue|wed|thu|fri|sat|sun> <note...>
      <amount> monthly <1-31> <note...>
    Raises ValueError(human-readable) on malformed input.
    """
    if len(args) < 2:
        raise ValueError("Usage: /recurring add <amount> <daily|weekly|monthly> [day] <note>")
    if not AMOUNT_RE.fullmatch(args[0]):
        raise ValueError(f"'{args[0]}' is not a valid amount.")
    amount_cents = dollars_to_cents(args[0])
    if amount_cents <= 0:
        raise ValueError("Amount must be greater than zero.")
    freq = args[1].lower()
    if freq == "daily":
        return amount_cents, "daily", None, " ".join(args[2:]).strip()
    if freq == "weekly":
        if len(args) < 3:
            raise ValueError("Weekly needs a day, e.g. /recurring add 10 weekly mon groceries")
        anchor = weekday_index(args[2])
        if anchor is None:
            raise ValueError(f"'{args[2]}' is not a weekday (mon, tue, …, sun).")
        return amount_cents, "weekly", anchor, " ".join(args[3:]).strip()
    if freq == "monthly":
        if len(args) < 3:
            raise ValueError("Monthly needs a day 1-31, e.g. /recurring add 1200 monthly 1 rent")
        if not args[2].isdigit() or not (1 <= int(args[2]) <= 31):
            raise ValueError(f"'{args[2]}' is not a day of month (1-31).")
        return amount_cents, "monthly", int(args[2]), " ".join(args[3:]).strip()
    raise ValueError("Frequency must be daily, weekly, or monthly.")


def parse_plain_text(text: str) -> tuple[int | None, str]:
    """Bare-message add ('kopi 2.80'). Stricter than parse_add: exactly one
    amount token AND a non-empty note, else (None, '') — callers stay silent."""
    tokens = text.strip().split()
    hits = [i for i, t in enumerate(tokens) if AMOUNT_RE.fullmatch(t)]
    if len(hits) != 1 or len(tokens) < 2:
        return None, ""
    i = hits[0]
    return dollars_to_cents(tokens[i]), " ".join(tokens[:i] + tokens[i + 1:]).strip()
