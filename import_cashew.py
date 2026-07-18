#!/usr/bin/env python3
"""One-time importer for a Cashew CSV export -> the expense-tracker DB.

Loads historical expenses (all accounts) so Trust email ingestion can run
go-forward only, with no overlap. Run ONCE, then delete/ignore this file.

  python import_cashew.py cashew-export.csv                 # imports into $DB_PATH
  python import_cashew.py cashew-export.csv --dry-run       # show, don't write
  python import_cashew.py --selftest                        # assert row transform

Skips income rows and unpaid/future rows (empty `amount`). Amounts are stored
as positive integer cents; Cashew's negative sign just marks an expense.
"""
from __future__ import annotations
import csv
import sys

from app import db
from app.config import DB_PATH

# Cashew category -> our 8, from the actual export's distinct values.
CASHEW_MAP = {
    "dining": "Food",
    "groceries": "Groceries",
    "transit": "Transport",
    "car rental": "Transport",
    "bills & fees": "Bills",
    "games": "Entertainment",
    "travel": "Entertainment",
    "entertainment": "Entertainment",
    "shopping": "Shopping",
    "beauty": "Health",
    "work": "Other",
}


def transform(row: dict) -> tuple[int, str, str, str] | None:
    """Cashew CSV row -> (amount_cents, note, category, created_at_iso), or None
    to skip (income, unpaid/future, non-SGD)."""
    if row["income"].strip().lower() == "true":
        return None
    amount = row["amount"].strip()
    if not amount:  # unpaid/future scheduled instance
        return None
    if row["currency"].strip() != "SGD":
        return None
    cents = int(round(abs(float(amount)) * 100))
    note = row["title"].strip() or row["note"].strip()
    category = CASHEW_MAP.get(row["category name"].strip().lower(), "Other")
    created_at = row["date"].strip().replace(" ", "T")[:19]  # "YYYY-MM-DDTHH:MM:SS"
    return cents, note, category, created_at


def run(csv_path: str, dry_run: bool) -> None:
    with open(csv_path, encoding="utf-8") as f:
        rows = [transform(r) for r in csv.DictReader(f)]
    rows = [r for r in rows if r is not None]
    print(f"{len(rows)} expense rows to import"
          f" (total {sum(c for c, *_ in rows) / 100:.2f} SGD).")
    if dry_run:
        for c, note, cat, ts in rows[:10]:
            print(f"  {ts}  {c/100:>8.2f}  {cat:<10} {note}")
        print("  ... (--dry-run: nothing written)")
        return
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    try:
        for cents, note, category, created_at in rows:
            db.add_transaction(conn, cents, note, category, "cashew-csv",
                               created_at=created_at)
    finally:
        conn.close()
    print(f"Imported {len(rows)} transactions into {DB_PATH}.")


def _selftest() -> None:
    sample = {"account": "Bank", "amount": "-6.25", "amount unpaid": "", "currency": "SGD",
              "title": "mcd", "note": "", "date": "2025-10-08 19:20:40.000", "income": "false",
              "type": "default", "category name": "Dining"}
    assert transform(sample) == (625, "mcd", "Food", "2025-10-08T19:20:40")
    assert transform({**sample, "income": "true"}) is None
    assert transform({**sample, "amount": ""}) is None
    assert transform({**sample, "category name": "Nonsense"})[2] == "Other"
    print("selftest ok")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        _selftest()
    elif args:
        run(args[0], dry_run="--dry-run" in args)
    else:
        raise SystemExit(__doc__)
