"""Archive a payslip PDF: parse it, write the file, index it in the DB.

This sits between `payslip` (pure parsing, no I/O) and the two front ends that
trigger an import — the Telegram bot and the web dashboard. It exists as its
own module for one reason: `web.py` must never import `bot.py`, which would
drag in python-telegram-bot. Without a shared home, the judgement calls below
(which figures are suspect, what counts as unreadable, how a failed decrypt is
handled) would be written twice and drift apart the first time either is tuned.

So this returns *findings*, not messages. The bot renders them as Telegram
text, the web returns them as JSON, and neither decides what is worth warning
about.

`db.py` must never import this (same circular-import rule as goals/portfolio/
tiger/fx/auth/payslip).
"""
from __future__ import annotations

import logging
import os

from app import db, payslip

log = logging.getLogger(__name__)


def archive_pdf(conn, data: bytes, password: str, dest_dir: str,
                fallback_period: str, source_uid: int | None = None,
                received_at: str | None = None) -> list[dict]:
    """Archive every payslip inside one PDF. Returns a result per month.

    One email can carry several months (a payslip-request reply held June and
    July 2026), so each is written under its own period with only its own
    pages. Re-importing is safe: `upsert_payslip` replaces on period.

    Each result carries `period`, `items`, `net_cents`, `gross_cents`,
    `text_source`, `reconciles`, `parsed`, and `warnings` — a list of plain
    sentences the caller can show as-is.
    """
    os.makedirs(dest_dir, exist_ok=True)
    try:
        slips = payslip.parse_pdf(data, password, fallback_period)
    except Exception:
        # Wrong password or an unreadable file. Keep the original bytes so the
        # document is never silently lost, and say the month is unparsed
        # rather than dropping it.
        log.exception("payslip decrypt/parse failed (uid=%s)", source_uid)
        path = os.path.join(dest_dir, f"{fallback_period}.pdf")
        with open(path, "wb") as f:
            f.write(data)
        db.upsert_payslip(conn, fallback_period, path, "", [],
                          source_uid=source_uid, received_at=received_at)
        return [{
            "period": fallback_period, "parsed": False, "items": [],
            "net_cents": None, "gross_cents": None, "text_source": None,
            "reconciles": None,
            "warnings": ["Couldn't read the payslip PDF — check "
                         "PAYSLIP_PDF_PASSWORD. The file was archived unparsed."],
        }]

    out = []
    for parsed in slips:
        path = os.path.join(dest_dir, f"{parsed['period']}.pdf")
        with open(path, "wb") as f:
            f.write(parsed["pdf"])
        db.upsert_payslip(conn, parsed["period"], path, parsed["raw_text"],
                          parsed["items"], net_cents=parsed["net_cents"],
                          gross_cents=parsed["gross_cents"],
                          source_uid=source_uid, received_at=received_at,
                          text_source=parsed["text_source"])
        out.append({
            "period": parsed["period"],
            "parsed": bool(parsed["items"]),
            "items": parsed["items"],
            "net_cents": parsed["net_cents"],
            "gross_cents": parsed["gross_cents"],
            "text_source": parsed["text_source"],
            "reconciles": parsed["reconciles"],
            "warnings": _warnings(parsed),
        })
    return out


def _warnings(parsed: dict) -> list[str]:
    """Everything about one parsed month that deserves a second look."""
    out = []
    if not parsed["raw_text"].strip():
        out.append("Nothing could be read from this PDF — no text layer and "
                   "OCR returned nothing.")
    elif not parsed["items"]:
        out.append("Text was read but no line items matched.")
    if parsed["period_source"] == "fallback":
        # A payslip covers the month BEFORE the one it is sent in, so this
        # guess is probably a month late. Say so rather than file it quietly
        # under a month that is likely wrong.
        out.append(f"Couldn't read the pay month — filed under "
                   f"{parsed['period']} from the email date. Check it.")
    if parsed["reconciles"] is False:
        # The payslip states its own totals, so this identity holding is the
        # main evidence OCR read every digit correctly. Failing it means a
        # figure is wrong, not that the payslip is.
        out.append("Gross − deductions doesn't equal net — a figure may have "
                   "been misread. Check against the PDF.")
    return out
