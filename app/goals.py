"""Savings-goal pace math and auto-funding.

Imports db; db must NEVER import this module (cycle).
"""
from __future__ import annotations
from datetime import date
import logging
import math

from app import db, recurrence

log = logging.getLogger(__name__)

PRIORITY_LABELS = {1: "high", 2: "normal", 3: "low"}

# Auto-funding always lands on the 1st. Not per-goal configurable.
# Deliberate: a per-goal anchor is another column + another input for no gain.
FUNDING_ANCHOR_DAY = 1


def first_of_next_month(today: date) -> str:
    """Funding cursor start: setting S$X/mo today does not charge today.
    Reuses recurrence's Dec->Jan rollover instead of a third copy of it."""
    year, month = recurrence._next_month(today.year, today.month)
    return date(year, month, 1).isoformat()


def months_left(target_date: date, today: date) -> int:
    """Calendar months from today's month through the target's, inclusive.
    A target in the current month is 1, so need_per_month never divides by zero."""
    return (target_date.year - today.year) * 12 + (target_date.month - today.month) + 1


def pace(target_cents, saved_cents, monthly_cents, target_date, today) -> dict:
    """Derived, never stored — so it self-corrects instead of drifting.
    Skip a month and remaining stays high while months_left drops, which
    pushes need_per_month up on its own. No expected-savings curve needed."""
    remaining = max(0, (target_cents or 0) - (saved_cents or 0))
    if remaining == 0:
        return {"status": "done", "months_left": None,
                "need_per_month": 0, "shortfall_per_month": 0}
    if target_date is None:
        return {"status": "none", "months_left": None,
                "need_per_month": None, "shortfall_per_month": 0}
    left = months_left(target_date, today)
    if left <= 0:
        return {"status": "overdue", "months_left": left,
                "need_per_month": remaining, "shortfall_per_month": remaining}
    need = math.ceil(remaining / left)
    shortfall = max(0, need - (monthly_cents or 0))
    return {"status": "ok" if shortfall == 0 else "behind", "months_left": left,
            "need_per_month": need, "shortfall_per_month": shortfall}


def list_with_pace(conn, today=None) -> list[dict]:
    today = today or date.today()
    out = []
    for g in db.list_goals(conn):
        target_date = date.fromisoformat(g["target_date"]) if g["target_date"] else None
        g["pace"] = pace(g["target_cents"], g["saved_cents"],
                         g["monthly_cents"], target_date, today)
        out.append(g)
    return out


def fund_due(conn, today: date) -> list[tuple[str, int]]:
    """Post every due monthly auto-contribution. Idempotent and back-filling:
    occurrences_through only ever moves the cursor forward, exactly as the
    recurring-transaction rules rely on.

    Each goal is wrapped in its own try/except so one bad goal can't skip
    every remaining goal in the sweep.
    """
    funded = []
    for row in db.goals_due_funding(conn, today.isoformat()):
        try:
            dates, new_next = recurrence.occurrences_through(
                "monthly", FUNDING_ANCHOR_DAY,
                date.fromisoformat(row["next_due"]), today)
            remaining = max(0, row["target_cents"] - row["saved"])
            total = 0
            for _ in dates:
                amount = min(row["monthly_cents"], remaining)
                if amount <= 0:
                    break          # target reached; never overshoot
                db.add_contribution(conn, row["id"], amount)
                remaining -= amount
                total += amount
            # Advance unconditionally — a clamped-to-zero goal must not be rescanned daily.
            # contributions commit individually and the cursor only advances
            # after the loop, so a process kill between the last insert and the cursor
            # write re-adds that back-fill on the next sweep. Not restructured into a
            # single transaction on purpose — the window is tiny and the worst case is
            # a duplicate contribution the user can delete, not lost money.
            db.update_goal(conn, row["id"], next_due=new_next.isoformat())
            if total:
                funded.append((row["name"], total))
        except Exception:
            log.exception("goal auto-funding failed for goal %s", row["id"])
    return funded
