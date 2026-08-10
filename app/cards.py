"""Resolve which card paid for a transaction, and learn from corrections.

Mirrors `app/categories.py`. The key is `(source, token)` rather than a bare
token: `A/C ending 1234` from DBS must not collide with the same digits from a
future issuer, and `token=''` expresses "everything from this sender is my one
card" -- the binding that makes attribution correct from the first import.

`db.py` must never import this (same circular-import rule as goals/portfolio/
tiger/fx/auth/payslip).
"""
from __future__ import annotations
import re

from app import db

_WS_RE = re.compile(r"\s+")
_EDGE = " \t\r\n.,;:"


def normalise_instrument(raw: str | None) -> str:
    """Lowercase, collapse internal whitespace, strip edge punctuation.

    Deliberately NOT `merchant_stem`: its reference-code detector keys on
    letter/digit run alternation and would mangle or discard exactly the shapes
    this depends on ("a/c ending 1234", "**** 4242"). Instruments are
    machine-generated and stable; they need no brand-key inference.
    """
    if not raw:
        return ""
    return _WS_RE.sub(" ", str(raw)).strip(_EDGE).strip().lower()


def effective_card(conn, source: str, token: str) -> tuple[int | None, str | None]:
    """(card_id, origin) for a spend. Origin is 'memory' | 'rule' | 'source' | None.

    Precedence, highest first:
      1. learned mapping (source, token)   -- the user corrected this instrument
      2. typed match rule on a card        -- the user seeded it up front
      3. learned source default (source,'')-- the user bound the whole sender
      4. None, i.e. Unassigned

    A learned mapping outranks a typed rule for the same reason
    `effective_category` puts merchant memory above the keyword rules:
    otherwise correcting a transaction the rule already matches would write a
    mapping that never takes effect, and the correction would appear to do
    nothing.

    An explicit card_id already on the transaction outranks all of this and is
    handled by the caller -- this function is only consulted for a new spend.
    """
    token = normalise_instrument(token)
    if token:
        remembered = db.recall_instrument(conn, source, token)
        if remembered is not None:
            return remembered, "memory"
        # Lowest id first, so a collision between two rules is deterministic
        # rather than depending on row order.
        for card in db.list_cards(conn):
            rule = normalise_instrument(card["match"])
            if rule and rule in token:
                return card["id"], "rule"
    default = db.recall_instrument(conn, source, "")
    if default is not None:
        return default, "source"
    return None, None


def learn_card_from_correction(conn, row, card_id: int | None) -> bool:
    """Teach the instrument mapping from a user's card correction.

    Returns True if anything was written. Only rows carrying an instrument can
    teach: free-form bot/dashboard notes have none, so the gate is naturally
    narrow and needs no source allowlist. Correcting TO Unassigned forgets the
    binding rather than doing nothing -- otherwise a wrong binding would be
    unremovable from the transaction list.
    """
    token = normalise_instrument(row["instrument"] if "instrument" in row.keys() else None)
    if not token or not row["source"]:
        return False
    if card_id is None:
        return db.forget_instrument(conn, row["source"], token)
    db.bind_instrument(conn, row["source"], token, card_id)
    return True


def assign_card(conn, tid, card_id: int | None) -> bool:
    """Set a transaction's card and learn from the change. False if no such row.

    The single home for this: `web.py` must never import `bot.py`, so without
    it the pre-update comparison and the learning call would be written twice
    and drift apart the first time either is tuned -- exactly why
    `payslip_archive` exists.

    Only a real CHANGE teaches. Both front ends re-send the current card on an
    unrelated save, so `card_id is not None` is dead as a guard; comparing
    against the stored value is what makes an amount-only edit leave the
    attribution alone.
    """
    row = db.get_transaction(conn, tid)
    if row is None:
        return False
    if row["card_id"] == card_id:
        return True
    # Archived cards are refused HERE, not in the caller. web.py had this guard
    # and the bot did not, so a picker message opened before a card was
    # archived could still bind an instrument that recall_instrument (which
    # joins on archived_at IS NULL) can never return -- silently inert
    # learning. The guard belongs with the write, or every new front end has to
    # remember it. web.py keeps its own check only to answer a nicer 400.
    if card_id is not None:
        card = db.get_card(conn, card_id)
        if card is None or card["archived_at"]:
            return False
    db.update_transaction(conn, tid, card_id=card_id)
    # The pre-update row is deliberate: learning reads `instrument` and
    # `source`, neither of which this write can touch, and passing it keeps
    # "we learn from the row as it was" visible instead of re-derivable.
    learn_card_from_correction(conn, row, card_id)
    return True
