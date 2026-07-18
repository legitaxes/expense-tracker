from __future__ import annotations
import asyncio
import html
import logging
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.constants import ParseMode
from datetime import date
from calendar import monthrange

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters)

from app import db, recurrence, trust_email
from app.config import (TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_ID, DB_PATH,
                        CATEGORIES, ALERT_THRESHOLDS,
                        GMAIL_USER, GMAIL_APP_PASSWORD, EMAIL_POLL_MINUTES,
                        EMAIL_CURSOR_PATH)
from app.parsing import parse_add, parse_recurring_add, parse_plain_text
from app.recurrence import weekday_name
from app.categories import guess_category
from app.money import dollars_to_cents, format_cents

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    level=logging.INFO)
log = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(r"(?:S\$|\$)?\d+(?:\.\d{1,2})?")

HELP = (
    "💸 <b>Expense Tracker</b>\n\n"
    "<b>/add</b> <code>12.50 lunch at hawker</code> — record a spend (category auto-guessed)\n"
    "<b>/income</b> <code>5000 july salary</code> — record income\n"
    "<b>/undo</b> — delete your last entry\n"
    "<b>/recent</b> <code>[n]</code> — last n entries with delete buttons\n"
    "<b>/summary</b> <code>[YYYY-MM]</code> — month total + breakdown\n"
    "<b>/find</b> <code>kopi</code> — search notes, delete from results\n"
    "<b>/budget</b> — list budgets vs spend\n"
    "<b>/budget</b> <code>Transport 300</code> — set a monthly limit (dollars)\n"
    "<b>/trip</b> <code>on|off</code> — overseas? new expenses default to Travel\n"
    "\n<b>/recurring</b> — list recurring rules\n"
    "<b>/recurring add</b> <code>1200 monthly 1 rent</code> — auto-log every month\n"
    "<b>/recurring add</b> <code>10 weekly mon groceries</code> · <code>5 daily coffee</code>\n"
    "<b>/recurring delete</b> <code>&lt;id&gt;</code> — stop a rule\n"
    "\n<b>/goal</b> — savings goals · <b>/goal add</b> <code>Japan 3000</code> · <b>/goal put</b> <code>Japan 200</code>\n"
)

CAT_EMOJI = {"Food": "🍜", "Groceries": "🛒", "Transport": "🚌", "Shopping": "🛍️",
             "Bills": "🧾", "Health": "💊", "Entertainment": "🎮", "Travel": "✈️",
             "Other": "📦"}


def is_authorized(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id == ALLOWED_CHAT_ID


def _looks_like_amount(s: str) -> bool:
    return bool(_AMOUNT_RE.fullmatch(s))


def _match_category(name: str):
    name = name.strip().lower()
    for c in CATEGORIES:
        if c.lower() == name:
            return c
    return None


def _conn():
    return db.connect(DB_PATH)


async def _guard(update: Update) -> bool:
    if is_authorized(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Not authorised.")
    return False


def _category_keyboard(tid: int) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, c in enumerate(CATEGORIES):
        row.append(InlineKeyboardButton(c, callback_data=f"cat:{tid}:{i}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def cmd_start(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_add(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    amount_cents, note = parse_add(" ".join(ctx.args))
    if amount_cents is None:
        if not ctx.args:
            await _send_quickadd(update, ctx)
        else:
            await update.message.reply_text("Usage: /add 12.50 lunch at hawker")
        return
    await _log_expense(update, amount_cents, note)


def _tx_buttons(tid, undo_tid=None):
    """Standard action row for any transaction message the bot posts."""
    rows = [[InlineKeyboardButton("✏️ Fix category", callback_data=f"fix:{tid}"),
             InlineKeyboardButton("✏️ Edit note", callback_data=f"note:{tid}")]]
    if undo_tid is not None:
        rows.append([InlineKeyboardButton("🗑 Undo", callback_data=f"del:{undo_tid}")])
    return InlineKeyboardMarkup(rows)


def _effective_category(conn, note: str) -> tuple[str, bool]:
    """(category, via_trip). Trip mode routes every new spend to Travel;
    recurring-rule postings and income flows deliberately bypass this."""
    if db.get_setting(conn, "trip_mode", "0") == "1":
        return "Travel", True
    return guess_category(note), False


def _cat_line(category: str, via_trip: bool) -> str:
    return f"🏷️ {category}" + (" · ✈️ trip mode" if via_trip else "")


async def _log_expense(update, amount_cents: int, note: str) -> None:
    ym = db.current_year_month()
    conn = _conn()
    try:
        category, via_trip = _effective_category(conn, note)
        tid = db.add_transaction(conn, amount_cents, note, category, "telegram")
        crossed = db.check_and_record_alerts(conn, category, ym, ALERT_THRESHOLDS)
        limit = db.get_budgets(conn).get(category)
        spent = db.month_category_spend(conn, ym, category)
    finally:
        conn.close()
    await update.message.reply_text(
        f"✅ Recorded <b>{format_cents(amount_cents)}</b>\n"
        f"📝 {note or '(no note)'}\n{_cat_line(category, via_trip)}",
        parse_mode=ParseMode.HTML,
        reply_markup=_tx_buttons(tid),
    )
    for _ in crossed:
        await update.message.reply_text(
            f"⚠️ <b>{category}</b> at {spent / limit * 100:.0f}% of {format_cents(limit)} "
            f"budget ({format_cents(spent)} this month).", parse_mode=ParseMode.HTML)


async def _send_quickadd(update, ctx) -> None:
    conn = _conn()
    try:
        freq = db.frequent_notes(conn)
    finally:
        conn.close()
    if not freq:
        await update.message.reply_text("Usage: /add 12.50 lunch at hawker")
        return
    # callback_data is 64 bytes — stash the list, buttons carry only an index
    ctx.application.bot_data["quickadd"] = freq
    buttons = [[InlineKeyboardButton(
        f"{CAT_EMOJI.get(guess_category(f['note']), '📦')} {f['note']} · "
        f"{format_cents(f['median_cents'])}",
        callback_data=f"qa:{i}")] for i, f in enumerate(freq)]
    await update.message.reply_text("Quick add — your usual suspects:",
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def cb_quickadd(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    freq = ctx.application.bot_data.get("quickadd")
    i = int(q.data.split(":")[1])
    if not freq or i >= len(freq):
        await q.edit_message_text("Menu expired — send /add again.")
        return
    f = freq[i]
    ym = db.current_year_month()
    conn = _conn()
    try:
        category, via_trip = _effective_category(conn, f["note"])
        tid = db.add_transaction(conn, f["median_cents"], f["note"], category, "telegram")
        crossed = db.check_and_record_alerts(conn, category, ym, ALERT_THRESHOLDS)
        limit = db.get_budgets(conn).get(category)
        spent = db.month_category_spend(conn, ym, category)
    finally:
        conn.close()
    await q.edit_message_text(
        f"✅ Recorded <b>{format_cents(f['median_cents'])}</b>\n"
        f"📝 {f['note']}\n{_cat_line(category, via_trip)}",
        parse_mode=ParseMode.HTML,
        reply_markup=_tx_buttons(tid, undo_tid=tid))
    for _ in crossed:
        await q.message.reply_text(
            f"⚠️ <b>{category}</b> at {spent / limit * 100:.0f}% of {format_cents(limit)} "
            f"budget ({format_cents(spent)} this month).", parse_mode=ParseMode.HTML)


async def cmd_trip(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg and arg not in ("on", "off"):
        await update.message.reply_text("Usage: /trip on | /trip off")
        return
    conn = _conn()
    try:
        if arg:
            db.set_setting(conn, "trip_mode", "1" if arg == "on" else "0")
        on = db.get_setting(conn, "trip_mode", "0") == "1"
    finally:
        conn.close()
    if on:
        await update.message.reply_text(
            "✈️ Trip mode is ON — new expenses (manual + email imports) default "
            "to Travel until /trip off. Recurring rules keep their categories.")
    else:
        await update.message.reply_text(
            "🏠 Trip mode is off — categories come from keyword guessing. "
            "/trip on when you head overseas.")


async def cmd_income(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    amount_cents, note = parse_add(" ".join(ctx.args))
    if amount_cents is None:
        await update.message.reply_text("Usage: /income 5000 july salary")
        return
    conn = _conn()
    try:
        db.add_transaction(conn, amount_cents, note, "Income", "telegram", type_="income")
    finally:
        conn.close()
    await update.message.reply_text(
        f"💰 Income recorded <b>{format_cents(amount_cents)}</b>\n📝 {note or '(no note)'}",
        parse_mode=ParseMode.HTML)


_COMMAND_WORDS = {"add", "income", "budget", "goal", "recurring", "find",
                  "summary", "recent", "undo", "help", "start"}


async def on_text(update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bare text like 'kopi 2.80' logs an expense — no /add needed.
    Anything that doesn't parse is ignored with NO reply (stray chat stays stray)."""
    if not is_authorized(update) or not update.message or not update.message.text:
        return
    amount_cents, note = parse_plain_text(update.message.text)
    if amount_cents is None:
        return
    first = update.message.text.split()[0].lower()
    if first in _COMMAND_WORDS:
        return  # forgot-the-slash command, not an expense
    await _log_expense(update, amount_cents, note)


async def cb_fix(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split(":")[1])
    await q.edit_message_reply_markup(_category_keyboard(tid))


async def cb_setcat(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    _, tid_s, idx_s = q.data.split(":")
    tid, category = int(tid_s), CATEGORIES[int(idx_s)]
    conn = _conn()
    try:
        db.update_transaction(conn, tid, category=category)
        row = db.get_transaction(conn, tid)
    finally:
        conn.close()
    if row:
        await q.edit_message_text(
            f"✅ Recorded <b>{format_cents(row['amount_cents'])}</b>\n"
            f"📝 {row['note'] or '(no note)'}\n🏷️ {category}", parse_mode=ParseMode.HTML,
            reply_markup=_tx_buttons(tid))


async def cb_editnote(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    tid = int(q.data.split(":")[1])
    conn = _conn()
    try:
        row = db.get_transaction(conn, tid)
    finally:
        conn.close()
    if row is None:
        await q.answer("That transaction is gone.", show_alert=True)
        return
    await q.answer()
    await q.message.reply_text(_edit_prompt(row),
                               reply_markup=ForceReply(selective=True))


async def on_edit_reply(update, ctx: ContextTypes.DEFAULT_TYPE):
    """Replies to bot messages. Edit-prompt replies update the note; any other
    reply falls through to plain-text add so 'kopi 2.80' still works there."""
    if not is_authorized(update) or not update.message or not update.message.text:
        return
    replied = update.message.reply_to_message
    tid = _parse_edit_prompt_tid(replied.text if replied else None)
    if tid is None or not replied.from_user or not replied.from_user.is_bot:
        await on_text(update, ctx)
        return
    new_note = update.message.text.strip()
    if not new_note:
        await update.message.reply_text("Note unchanged — send some text.")
        return
    conn = _conn()
    try:
        ok = db.update_transaction(conn, tid, note=new_note)
        row = db.get_transaction(conn, tid) if ok else None
    finally:
        conn.close()
    if row is None:
        await update.message.reply_text("That transaction is gone.")
        return
    await update.message.reply_text(
        f"📝 Updated #{tid}: “{new_note}” — "
        f"<b>{format_cents(row['amount_cents'])}</b> 🏷️ {row['category']}",
        parse_mode=ParseMode.HTML, reply_markup=_tx_buttons(tid))


async def cmd_undo(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    conn = _conn()
    try:
        tid = db.latest_transaction_id(conn)
        if tid is None:
            await update.message.reply_text("Nothing to undo.")
            return
        row = db.get_transaction(conn, tid)
        db.delete_transaction(conn, tid)
    finally:
        conn.close()
    await update.message.reply_text(
        f"↩️ Deleted: {format_cents(row['amount_cents'])} — "
        f"{row['note'] or '(no note)'} ({row['category']})")


async def cmd_recent(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    n = min(int(ctx.args[0]), 20) if ctx.args and ctx.args[0].isdigit() else 5
    conn = _conn()
    try:
        rows = db.list_transactions(conn, limit=n)
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text("No transactions yet.")
        return
    for r in rows:
        await update.message.reply_text(
            f"<b>{format_cents(r['amount_cents'])}</b> — {r['note'] or '(no note)'}\n"
            f"🏷️ {r['category']} · {r['created_at'][:16].replace('T', ' ')}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Delete", callback_data=f"del:{r['id']}")]]))


async def cmd_find(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("Usage: /find kopi")
        return
    conn = _conn()
    try:
        rows = db.search_transactions(conn, query)
    finally:
        conn.close()
    if not rows:
        await update.message.reply_text(f"No matches for \"{query}\".")
        return
    lines = [f"{i + 1}. {r['created_at'][:10]} · {r['note'] or '(no note)'} · "
             f"<b>{format_cents(r['amount_cents'])}</b>"
             for i, r in enumerate(rows)]
    buttons = [InlineKeyboardButton(f"🗑 {i + 1}", callback_data=f"del:{r['id']}")
               for i, r in enumerate(rows)]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [buttons[i:i + 4] for i in range(0, len(buttons), 4)]))


async def cb_del(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split(":")[1])
    conn = _conn()
    try:
        row = db.get_transaction(conn, tid)
        db.delete_transaction(conn, tid)
    finally:
        conn.close()
    if row is None:
        await q.edit_message_text("Already deleted.")
    else:
        await q.edit_message_text(
            f"🗑 Deleted {format_cents(row['amount_cents'])} — "
            f"{row['note'] or '(no note)'} 🏷️ {row['category']}")


def _summary_lines(s, income: int) -> list[str]:
    lines = [f"📊 <b>{s['month']}</b> — total <b>{format_cents(s['total_cents'])}</b>\n"]
    lines += [f"• {i['category']}: {format_cents(i['total_cents'])}" for i in s["by_category"]]
    if income:
        net = income - s["total_cents"]
        lines.append(f"\n💰 Income: {format_cents(income)}")
        lines.append(f"{'✅' if net >= 0 else '🔻'} Net: {format_cents(net)}")
    return lines


async def cmd_summary(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    month = ctx.args[0] if ctx.args else db.current_year_month()
    conn = _conn()
    try:
        s = db.month_summary(conn, month)
        income = db.month_income(conn, month)
    finally:
        conn.close()
    if not s["by_category"] and not income:
        await update.message.reply_text(f"No activity recorded for {month}.")
        return
    await update.message.reply_text("\n".join(_summary_lines(s, income)),
                                    parse_mode=ParseMode.HTML)


async def cmd_budget(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = ctx.args
    conn = _conn()
    try:
        if len(args) >= 2 and _looks_like_amount(args[-1]):
            category = _match_category(" ".join(args[:-1]))
            if category is None:
                await update.message.reply_text("Unknown category. Valid: " + ", ".join(CATEGORIES))
                return
            cents = dollars_to_cents(args[-1])
            db.set_budget(conn, category, cents)
            await update.message.reply_text(
                f"💰 Budget for <b>{category}</b> set to {format_cents(cents)}/month.",
                parse_mode=ParseMode.HTML)
            return
        budgets = db.get_budgets(conn)
        if not budgets:
            await update.message.reply_text(
                "No budgets set. Use /budget <category> <amount>, e.g. /budget Transport 300")
            return
        ym = db.current_year_month()
        lines = ["💰 <b>Budgets (this month)</b>\n"]
        for cat, limit in budgets.items():
            spent = db.month_category_spend(conn, ym, cat)
            pct = spent / limit * 100 if limit else 0
            lines.append(f"• {cat}: {format_cents(spent)} / {format_cents(limit)} ({pct:.0f}%)")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    finally:
        conn.close()


_EDIT_PROMPT_RE = re.compile(r"new note for #(\d+)")


def _edit_prompt(row) -> str:
    return (f"✏️ Reply with the new note for #{row['id']}\n"
            f"({format_cents(row['amount_cents'])} — {row['note'] or '(no note)'})")


def _parse_edit_prompt_tid(text):
    m = _EDIT_PROMPT_RE.search(text or "")
    return int(m.group(1)) if m else None


def _digest_lines(rows):
    """End-of-day digest body; None = nothing to send."""
    if not rows:
        return None
    total = sum(r["amount_cents"] for r in rows if r["type"] == "expense")
    lines = [f"🌙 <b>Today</b> — {len(rows)} txns, <b>{format_cents(total)}</b>\n"]
    for r in rows:
        mark = "✉ " if r["source"].endswith("-email") else ""
        plus = "💰 +" if r["type"] == "income" else ""
        lines.append(f"• {plus}{format_cents(r['amount_cents'])} {mark}{r['note']}"
                     f" → {r['category']}")
    return lines


def _describe_rule(rule) -> str:
    freq = rule["frequency"]
    if freq == "daily":
        when = "daily"
    elif freq == "weekly":
        when = "every " + weekday_name(rule["anchor"]).capitalize()
    else:
        when = f"monthly on day {rule['anchor']}"
    return (f"#{rule['id']} <b>{format_cents(rule['amount_cents'])}</b> — "
            f"{rule['note'] or '(no note)'} [{rule['category']}]\n"
            f"   ↳ {when} · next {rule['next_due']}")


def _goal_bar(pct: float) -> str:
    filled = min(10, int(pct // 10))
    return "▰" * filled + "▱" * (10 - filled)


def _describe_goal(g) -> str:
    pct = g["saved_cents"] / g["target_cents"] * 100 if g["target_cents"] else 0
    return (f"#{g['id']} <b>{html.escape(g['name'])}</b>\n"
            f"   {_goal_bar(pct)} {pct:.0f}%\n"
            f"   {format_cents(g['saved_cents'])} of {format_cents(g['target_cents'])}")


async def _post_rule(bot, conn, rule, today: date) -> int:
    """Post every due occurrence for one rule, advance next_due, alert, notify.
    Returns the number of occurrences posted."""
    dates, new_next = recurrence.occurrences_through(
        rule["frequency"], rule["anchor"], date.fromisoformat(rule["next_due"]), today)
    if not dates:
        return 0
    posted_ids = []
    for d in dates:
        posted_ids.append(db.add_transaction(
            conn, rule["amount_cents"], rule["note"], rule["category"],
            "recurring", created_at=d.isoformat() + "T00:00:00"))
    # Advance the cursor BEFORE notifying, deliberately: the record + cursor advance is
    # the source of truth and must stay idempotent. If a send below fails it is logged by
    # post_due_recurring's per-rule guard — a missed notification is acceptable; a
    # re-posted (duplicate) expense on the next tick would not be.
    db.set_next_due(conn, rule["id"], new_next.isoformat())

    if len(dates) == 1:
        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=(f"🔁 Recurring posted <b>{format_cents(rule['amount_cents'])}</b>\n"
                  f"📝 {rule['note'] or '(no note)'}\n"
                  f"🏷️ {rule['category']} · {dates[0].isoformat()}"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🗑 Undo", callback_data=f"del:{posted_ids[0]}")]]))
    else:
        total = rule["amount_cents"] * len(dates)
        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=(f"🔁 Recurring back-fill: <b>{len(dates)}×</b> "
                  f"{rule['note'] or '(no note)'} ({format_cents(rule['amount_cents'])} each)\n"
                  f"🏷️ {rule['category']} · {dates[0].isoformat()} → {dates[-1].isoformat()}\n"
                  f"Σ <b>{format_cents(total)}</b>"),
            parse_mode=ParseMode.HTML)

    ym = db.current_year_month()
    crossed = db.check_and_record_alerts(conn, rule["category"], ym, ALERT_THRESHOLDS)
    if crossed:
        limit = db.get_budgets(conn).get(rule["category"])
        spent = db.month_category_spend(conn, ym, rule["category"])
        for _t in crossed:
            await bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=(f"⚠️ <b>{rule['category']}</b> at {spent / limit * 100:.0f}% of "
                      f"{format_cents(limit)} budget ({format_cents(spent)} this month)."),
                parse_mode=ParseMode.HTML)
    return len(dates)


async def post_due_recurring(app) -> None:
    """Scheduler tick + startup catch-up: post all rules whose next_due has arrived."""
    today = date.today()
    conn = _conn()
    try:
        for rule in db.due_recurring(conn, today.isoformat()):
            try:
                await _post_rule(app.bot, conn, rule, today)
            except Exception:
                log.exception("failed to post recurring rule %s", rule["id"])
    finally:
        conn.close()


async def _handle_import(bot, conn, uid, sender, subject, html, msg_dt) -> None:
    """Insert one parsed transaction email + notify; or ping if a real spend
    won't parse. Trust cancellations warn instead of inserting."""
    label, source = trust_email.SOURCES.get(sender, ("Trust", "trust-email"))
    action, parsed = trust_email.classify_email(sender, subject, html, msg_dt)
    if action == "cancelled":
        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=(f"↩️ Trust cancelled {format_cents(parsed['amount_cents'])} at "
                  f"{parsed['note']} — if it was imported earlier, remove it "
                  f"with /undo or on the dashboard."))
        return
    if action == "unparsed":
        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=(f"⚠️ Couldn't parse a {label} transaction email — add a pattern.\n"
                  f"Subject: {subject}"))
        return
    if action == "suggest":
        # Receipt-looking email from a sender we have no parser for: ask first.
        pid = db.add_pending_import(conn, parsed["amount_cents"], parsed["note"],
                                    msg_dt.strftime("%Y-%m-%dT%H:%M:%S"))
        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=(f"🧾 Possible receipt <b>{format_cents(parsed['amount_cents'])}</b>\n"
                  f"✉️ {sender}\n📝 {parsed['note']}"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Add", callback_data=f"imp:{pid}"),
                InlineKeyboardButton("❌ Ignore", callback_data=f"ign:{pid}"),
            ]]))
        return
    if action == "suggest_income":
        pid = db.add_pending_import(conn, parsed["amount_cents"], parsed["note"],
                                    msg_dt.strftime("%Y-%m-%dT%H:%M:%S"), type_="income")
        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=(f"💰 Incoming money <b>{format_cents(parsed['amount_cents'])}</b>\n"
                  f"✉️ {sender}\n📝 {parsed['note']}"),
            parse_mode=ParseMode.HTML,
            reply_markup=_reimb_suggest_buttons(pid))
        return
    if action != "import":
        return  # non-transaction (statement/notice/dedup) → silently skip
    category, via_trip = _effective_category(conn, parsed["note"])
    tid = db.add_transaction(conn, parsed["amount_cents"], parsed["note"],
                             category, source, created_at=parsed["created_at"])
    await bot.send_message(
        chat_id=ALLOWED_CHAT_ID,
        text=(f"📥 Imported from {label} <b>{format_cents(parsed['amount_cents'])}</b>\n"
              f"💳 {parsed['note']}\n{_cat_line(category, via_trip)}"),
        parse_mode=ParseMode.HTML,
        reply_markup=_tx_buttons(tid))
    ym = parsed["created_at"][:7]
    crossed = db.check_and_record_alerts(conn, category, ym, ALERT_THRESHOLDS)
    if crossed:
        limit = db.get_budgets(conn).get(category)
        spent = db.month_category_spend(conn, ym, category)
        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=(f"⚠️ <b>{category}</b> at {spent / limit * 100:.0f}% of "
                  f"{format_cents(limit)} budget ({format_cents(spent)} this month)."),
            parse_mode=ParseMode.HTML)


async def cb_import_accept(update, ctx: ContextTypes.DEFAULT_TYPE):
    """"✅ Add" on a suggested receipt: insert it, then offer the Fix flow."""
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":")[1])
    conn = _conn()
    try:
        row = db.get_pending_import(conn, pid)
        if row is None:
            await q.edit_message_text("Already handled.")
            return
        if row["type"] == "income":
            db.add_transaction(conn, row["amount_cents"], row["note"], "Income",
                               "suggest-email", created_at=row["created_at"], type_="income")
            db.delete_pending_import(conn, pid)
            category, tid, via_trip = "Income", None, False
        else:
            category, via_trip = _effective_category(conn, row["note"])
            tid = db.add_transaction(conn, row["amount_cents"], row["note"], category,
                                     "suggest-email", created_at=row["created_at"])
            db.delete_pending_import(conn, pid)
    finally:
        conn.close()
    markup = _tx_buttons(tid) if tid else None
    await q.edit_message_text(
        f"📥 Imported <b>{format_cents(row['amount_cents'])}</b>\n"
        f"📝 {row['note']}\n{_cat_line(category, via_trip)}",
        parse_mode=ParseMode.HTML, reply_markup=markup)


async def cb_import_ignore(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":")[1])
    conn = _conn()
    try:
        row = db.get_pending_import(conn, pid)
        db.delete_pending_import(conn, pid)
    finally:
        conn.close()
    if row is None:
        await q.edit_message_text("Already handled.")
    else:
        await q.edit_message_text(
            f"🚫 Ignored {format_cents(row['amount_cents'])} — {row['note']}")


def _reimb_confirm(amount_cents, original, links) -> str:
    reimbursed = sum(-l["amount_cents"] for l in links)
    net = original["amount_cents"] - reimbursed
    msg = (f"↩️ Reimbursed <b>{format_cents(amount_cents)}</b> → "
           f"{original['note'] or '(no note)'} "
           f"({format_cents(original['amount_cents'])} → net {format_cents(net)}) "
           f"🏷️ {original['category']}")
    if net < 0:
        msg += "\n⚠ Reimbursed more than the original."
    return msg


def _reimb_suggest_buttons(pid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Reimburse", callback_data=f"rm:{pid}"),
        InlineKeyboardButton("💰 Income", callback_data=f"imp:{pid}"),
        InlineKeyboardButton("❌ Ignore", callback_data=f"ign:{pid}"),
    ]])


def _reimb_cat_keyboard(pid):
    rows, row = [], []
    for i, c in enumerate(CATEGORIES):
        row.append(InlineKeyboardButton(f"{CAT_EMOJI.get(c, '📦')} {c}",
                                        callback_data=f"rc:{pid}:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def cb_reimb(update, ctx: ContextTypes.DEFAULT_TYPE):
    """↩️ Reimburse on an incoming-money suggestion: show the link picker."""
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":")[1])
    conn = _conn()
    try:
        row = db.get_pending_import(conn, pid)
        cands = db.recent_expense_candidates(conn, row["amount_cents"]) if row else []
    finally:
        conn.close()
    if row is None:
        await q.edit_message_text("Already handled.")
        return
    if not cands:
        await q.edit_message_reply_markup(_reimb_cat_keyboard(pid))
        return
    buttons = [[InlineKeyboardButton(
        f"{CAT_EMOJI.get(c['category'], '📦')} {format_cents(c['amount_cents'])} "
        f"{(c['note'] or '(no note)')[:28]}",
        callback_data=f"rl:{pid}:{c['id']}")] for c in cands]
    buttons.append([InlineKeyboardButton("🏷 No match — pick category",
                                         callback_data=f"rmc:{pid}")])
    buttons.append([InlineKeyboardButton("◀ Back", callback_data=f"rmb:{pid}")])
    await q.edit_message_reply_markup(InlineKeyboardMarkup(buttons))


async def cb_reimb_link(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    _, pid_s, otid_s = q.data.split(":")
    pid, otid = int(pid_s), int(otid_s)
    conn = _conn()
    try:
        row = db.get_pending_import(conn, pid)
        orig = db.get_transaction(conn, otid)
        if row is None or orig is None:
            await q.edit_message_text("Already handled.")
            return
        db.add_transaction(conn, -row["amount_cents"], "↩️ " + row["note"],
                           orig["category"], "suggest-email",
                           created_at=row["created_at"], link_tid=otid)
        db.delete_pending_import(conn, pid)
        links = db.get_links(conn, otid)
    finally:
        conn.close()
    await q.edit_message_text(_reimb_confirm(row["amount_cents"], orig, links),
                              parse_mode=ParseMode.HTML)


async def cb_reimb_cat_menu(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":")[1])
    await q.edit_message_reply_markup(_reimb_cat_keyboard(pid))


async def cb_reimb_setcat(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    _, pid_s, idx_s = q.data.split(":")
    pid, category = int(pid_s), CATEGORIES[int(idx_s)]
    conn = _conn()
    try:
        row = db.get_pending_import(conn, pid)
        if row is None:
            await q.edit_message_text("Already handled.")
            return
        db.add_transaction(conn, -row["amount_cents"], "↩️ " + row["note"],
                           category, "suggest-email", created_at=row["created_at"])
        db.delete_pending_import(conn, pid)
    finally:
        conn.close()
    await q.edit_message_text(
        f"↩️ Reimbursed <b>{format_cents(row['amount_cents'])}</b> 🏷️ {category} "
        f"(no linked transaction)", parse_mode=ParseMode.HTML)


async def cb_reimb_back(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":")[1])
    await q.edit_message_reply_markup(_reimb_suggest_buttons(pid))


async def poll_trust_email(app) -> None:
    """Scheduler tick: pull new transaction emails (Trust/Grab/PayLah) over IMAP
    and import them. First run seeds the cursor to the newest email and imports
    nothing (go-forward)."""
    if not GMAIL_APP_PASSWORD:
        return
    cursor = trust_email.read_cursor(EMAIL_CURSOR_PATH)
    try:
        if cursor is None:  # never seeded → go-forward from now
            validity, newest = await asyncio.to_thread(
                trust_email.seed_max_uid, trust_email.IMAP_HOST, GMAIL_USER, GMAIL_APP_PASSWORD)
            trust_email.write_cursor(EMAIL_CURSOR_PATH, validity, newest)
            log.info("Trust email cursor seeded at UID %s (go-forward).", newest)
            return
        validity, emails = await asyncio.to_thread(
            trust_email.fetch_since, trust_email.IMAP_HOST, GMAIL_USER,
            GMAIL_APP_PASSWORD, cursor[1])
    except Exception:
        log.exception("Trust email poll failed")
        return
    if validity != cursor[0]:  # Gmail UIDVALIDITY changed → reseed, don't backfill
        _, newest = await asyncio.to_thread(
            trust_email.seed_max_uid, trust_email.IMAP_HOST, GMAIL_USER, GMAIL_APP_PASSWORD)
        trust_email.write_cursor(EMAIL_CURSOR_PATH, validity, newest)
        log.warning("UIDVALIDITY changed; reseeded cursor at UID %s.", newest)
        return
    conn = _conn()
    try:
        for uid, sender, subject, html, msg_dt in emails:
            try:
                await _handle_import(app.bot, conn, uid, sender, subject, html, msg_dt)
            except Exception:
                log.exception("failed to import transaction email UID %s", uid)
            # Advance AFTER handling so a crash re-processes from here, never past.
            trust_email.write_cursor(EMAIL_CURSOR_PATH, validity, uid)
    finally:
        conn.close()


async def send_daily_digest(app) -> None:
    """23:59 SGT roundup of everything recorded today (✉ marks email imports)."""
    today = date.today().isoformat()
    conn = _conn()
    try:
        rows = db.list_transactions(conn, date_from=today + "T00:00:00", date_to=today)
    finally:
        conn.close()
    lines = _digest_lines(rows)
    if lines is None:
        return
    await app.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="\n".join(lines),
                               parse_mode=ParseMode.HTML)


def _prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


async def send_month_report(app) -> None:
    """1st of month, 9am SGT: report card for the month that just ended."""
    ym = _prev_month(db.current_year_month())
    prev = _prev_month(ym)
    conn = _conn()
    try:
        s = db.month_summary(conn, ym)
        s_prev = db.month_summary(conn, prev)
        income = db.month_income(conn, ym)
        merchants = db.merchant_leaderboard(conn, ym, limit=5)
        budgets = db.get_budgets(conn)
        spends = {c: db.month_category_spend(conn, ym, c) for c in budgets}
        big = conn.execute(
            "SELECT note, amount_cents FROM transactions "
            "WHERE substr(created_at,1,7)=? AND type='expense' "
            "ORDER BY amount_cents DESC LIMIT 1", (ym,)).fetchone()
    finally:
        conn.close()
    delta = s["total_cents"] - s_prev["total_cents"]
    arrow = "▲" if delta > 0 else "▼"
    lines = [f"🗓 <b>{ym} report card</b>",
             f"Spent <b>{format_cents(s['total_cents'])}</b> "
             f"({arrow} {format_cents(abs(delta))} vs {prev})"]
    if income:
        lines.append(f"💰 Income {format_cents(income)} · "
                     f"Net {format_cents(income - s['total_cents'])}")
    if big:
        lines.append(f"💸 Biggest: {big['note'] or '(no note)'} "
                     f"{format_cents(big['amount_cents'])}")
    if merchants:
        lines.append("\n<b>Top merchants</b>")
        lines += [f"• {m['merchant']} — {format_cents(m['total_cents'])} ({m['count']}×)"
                  for m in merchants]
    if budgets:
        lines.append("\n<b>Budgets</b>")
        for cat, limit in budgets.items():
            pct = spends[cat] / limit * 100 if limit else 0
            mark = "✅" if pct < 80 else "⚠️" if pct <= 100 else "❌"
            lines.append(f"{mark} {cat}: {format_cents(spends[cat])} of {format_cents(limit)}")
    await app.bot.send_message(chat_id=ALLOWED_CHAT_ID, text="\n".join(lines),
                               parse_mode=ParseMode.HTML)
    await send_goal_nudge(app, ym)


async def send_goal_nudge(app, ym: str) -> None:
    """Second message after the report: offer to bank last month's leftover."""
    conn = _conn()
    try:
        leftover = db.month_income(conn, ym) - db.month_summary(conn, ym)["total_cents"]
        goals = [g for g in db.list_goals(conn) if g["saved_cents"] < g["target_cents"]]
    finally:
        conn.close()
    if leftover <= 0 or not goals:
        return
    buttons = [[InlineKeyboardButton(
        f"+{format_cents(min(leftover, g['target_cents'] - g['saved_cents']))} → {g['name']}",
        callback_data=f"gn:{g['id']}:{min(leftover, g['target_cents'] - g['saved_cents'])}")]
        for g in goals[:3]]
    buttons.append([InlineKeyboardButton("Skip", callback_data="gn:skip")])
    await app.bot.send_message(
        chat_id=ALLOWED_CHAT_ID,
        text=f"🎯 {ym} left <b>{format_cents(leftover)}</b> unspent — save it?",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def cb_goal_nudge(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    if parts[1] == "skip":
        await q.edit_message_text("👍 Skipped — nothing allocated to goals this month.")
        return
    gid, cents = int(parts[1]), int(parts[2])
    conn = _conn()
    try:
        if db.get_goal(conn, gid) is None:
            await q.edit_message_text("Goal gone.")
            return
        db.add_contribution(conn, gid, cents)
        goals = db.list_goals(conn)
    finally:
        conn.close()
    g = next(x for x in goals if x["id"] == gid)
    await q.edit_message_text(
        f"🎯 Added <b>{format_cents(cents)}</b> to {html.escape(g['name'])} — "
        f"{format_cents(g['saved_cents'])} of {format_cents(g['target_cents'])}.",
        parse_mode=ParseMode.HTML)


async def check_pace(app) -> None:
    """Daily 8pm: warn when a budget is pacing past 110% by month-end.
    Starts day 10 (earlier projections are noise); the existing 100% alert
    owns anything already over."""
    today = date.today()
    if today.day < 10:
        return
    ym = db.current_year_month()
    days = monthrange(today.year, today.month)[1]
    conn = _conn()
    try:
        for cat, limit in db.get_budgets(conn).items():
            spent = db.month_category_spend(conn, ym, cat)
            if spent >= limit:
                continue
            projected = spent * days // today.day
            if projected * 100 <= limit * 110:
                continue
            if not db.record_pace_alert(conn, cat, ym):
                continue
            await app.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=(f"📈 <b>{cat}</b> pacing {format_cents(projected)} by month-end "
                      f"(budget {format_cents(limit)})."),
                parse_mode=ParseMode.HTML)
    finally:
        conn.close()


async def cmd_recurring(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = ctx.args
    sub = args[0].lower() if args else "list"

    if sub == "add":
        try:
            amount_cents, freq, anchor, note = parse_recurring_add(args[1:])
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        category = guess_category(note)
        today = date.today()
        next_due = recurrence.first_due(freq, anchor, today)
        conn = _conn()
        try:
            rid = db.add_recurring(conn, amount_cents, note, category, freq, anchor,
                                   next_due.isoformat())
            rule = db.get_recurring(conn, rid)
            await update.message.reply_text(
                "✅ Recurring rule created:\n" + _describe_rule(rule),
                parse_mode=ParseMode.HTML)
            # If it's already due today, post it now (instant feedback); else it waits.
            await _post_rule(ctx.bot, conn, rule, today)
        finally:
            conn.close()
        return

    if sub in ("delete", "del", "remove", "rm"):
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Usage: /recurring delete <id>  (see /recurring list)")
            return
        rid = int(args[1])
        conn = _conn()
        try:
            ok = db.deactivate_recurring(conn, rid)
        finally:
            conn.close()
        await update.message.reply_text(
            f"🗑 Deleted recurring rule #{rid}." if ok else f"No active rule #{rid}.")
        return

    # default: list
    conn = _conn()
    try:
        rules = db.list_recurring(conn)
    finally:
        conn.close()
    if not rules:
        await update.message.reply_text(
            "No recurring rules yet. Add one:\n"
            "/recurring add 1200 monthly 1 rent\n"
            "/recurring add 10 weekly mon groceries\n"
            "/recurring add 5 daily coffee")
        return
    for r in rules:
        await update.message.reply_text(
            _describe_rule(r), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🗑 Delete", callback_data=f"recdel:{r['id']}")]]))


async def cb_recdel(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    q = update.callback_query
    await q.answer()
    rid = int(q.data.split(":")[1])
    conn = _conn()
    try:
        row = db.get_recurring(conn, rid)
        ok = db.deactivate_recurring(conn, rid)
    finally:
        conn.close()
    await q.edit_message_text(
        f"🗑 Recurring deleted: {_describe_rule(row)}" if ok and row
        else f"No active rule #{rid}.")


async def cmd_goal(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = ctx.args
    sub = args[0].lower() if args else "list"
    conn = _conn()
    try:
        if sub == "add":
            # /goal add Japan Trip 3000  — last token is the target
            if len(args) < 3 or not _looks_like_amount(args[-1]):
                await update.message.reply_text("Usage: /goal add <name> <target>, e.g. /goal add Japan 3000")
                return
            name = " ".join(args[1:-1]).strip()
            gid = db.add_goal(conn, name, dollars_to_cents(args[-1]))
            g = next(x for x in db.list_goals(conn) if x["id"] == gid)
            await update.message.reply_text("🎯 Goal created:\n" + _describe_goal(g),
                                            parse_mode=ParseMode.HTML)
            return
        if sub == "put":
            # /goal put Japan 200 — last token is the amount
            if len(args) < 3 or not _looks_like_amount(args[-1]):
                await update.message.reply_text("Usage: /goal put <name> <amount>, e.g. /goal put Japan 200")
                return
            goal = db.find_goal(conn, " ".join(args[1:-1]).strip())
            if goal is None:
                names = ", ".join(g["name"] for g in db.list_goals(conn)) or "none yet"
                await update.message.reply_text(f"No unique goal match. Goals: {names}")
                return
            db.add_contribution(conn, goal["id"], dollars_to_cents(args[-1]))
            g = next(x for x in db.list_goals(conn) if x["id"] == goal["id"])
            await update.message.reply_text("💪 Added!\n" + _describe_goal(g),
                                            parse_mode=ParseMode.HTML)
            return
        if sub in ("delete", "del", "remove", "rm"):
            if len(args) < 2 or not args[1].isdigit():
                await update.message.reply_text("Usage: /goal delete <id>  (see /goal)")
                return
            ok = db.delete_goal(conn, int(args[1]))
            await update.message.reply_text(
                f"🗑 Goal #{args[1]} deleted." if ok else f"No goal #{args[1]}.")
            return
        # default: list
        goals = db.list_goals(conn)
        if not goals:
            await update.message.reply_text(
                "No goals yet. Create one:\n/goal add Japan 3000\nThen contribute: /goal put Japan 200")
            return
        await update.message.reply_text(
            "🎯 <b>Savings goals</b>\n\n" + "\n\n".join(_describe_goal(g) for g in goals),
            parse_mode=ParseMode.HTML)
    finally:
        conn.close()


async def _on_startup(app) -> None:
    """post_init hook — runs once inside the bot's event loop before polling starts.
    Lives only in expense-bot, so recurring rules post exactly once per due date."""
    await app.bot.set_my_commands([
        ("add", "Record a spend: /add 12.50 lunch at hawker"),
        ("income", "Record income: /income 5000 salary"),
        ("undo", "Delete the latest transaction"),
        ("recent", "Show recent transactions"),
        ("find", "Search transactions: /find kopi"),
        ("summary", "Month spending by category"),
        ("budget", "Show/set budgets: /budget Transport 300"),
        ("trip", "Trip mode: new spends default to Travel"),
        ("recurring", "Auto-logged expenses: add/list/delete"),
        ("goal", "Savings goals: add/put/delete"),
        ("help", "How everything works"),
    ])
    scheduler = AsyncIOScheduler(timezone="Asia/Singapore")
    scheduler.add_job(post_due_recurring, "cron", hour=0, minute=10, args=[app],
                      id="recurring-tick", replace_existing=True)
    scheduler.add_job(send_month_report, "cron", day=1, hour=9, minute=0, args=[app],
                      id="month-report", replace_existing=True)
    if GMAIL_APP_PASSWORD:
        scheduler.add_job(poll_trust_email, "interval", minutes=EMAIL_POLL_MINUTES,
                          args=[app], id="trust-email-poll", replace_existing=True)
    scheduler.add_job(send_daily_digest, "cron", hour=23, minute=59, args=[app],
                      id="daily-digest", replace_existing=True)
    scheduler.add_job(check_pace, "cron", hour=20, minute=0, args=[app],
                      id="pace-check", replace_existing=True)
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    await post_due_recurring(app)  # catch up anything missed while we were down
    if GMAIL_APP_PASSWORD:
        await poll_trust_email(app)  # seed on first boot / catch up since last run
    log.info("Schedulers started; catch-up complete.")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set — see .env.example")
    if not ALLOWED_CHAT_ID:
        log.warning("ALLOWED_CHAT_ID is 0 — the bot will reject every message.")
    conn = _conn()
    db.init_schema(conn)
    conn.close()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_on_startup).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("income", cmd_income))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("trip", cmd_trip))
    app.add_handler(CommandHandler("recurring", cmd_recurring))
    app.add_handler(CommandHandler("goal", cmd_goal))
    app.add_handler(CallbackQueryHandler(cb_fix, pattern=r"^fix:"))
    app.add_handler(CallbackQueryHandler(cb_setcat, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(cb_del, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(cb_recdel, pattern=r"^recdel:"))
    app.add_handler(CallbackQueryHandler(cb_import_accept, pattern=r"^imp:"))
    app.add_handler(CallbackQueryHandler(cb_import_ignore, pattern=r"^ign:"))
    app.add_handler(CallbackQueryHandler(cb_reimb, pattern=r"^rm:"))
    app.add_handler(CallbackQueryHandler(cb_reimb_link, pattern=r"^rl:"))
    app.add_handler(CallbackQueryHandler(cb_reimb_cat_menu, pattern=r"^rmc:"))
    app.add_handler(CallbackQueryHandler(cb_reimb_setcat, pattern=r"^rc:"))
    app.add_handler(CallbackQueryHandler(cb_reimb_back, pattern=r"^rmb:"))
    app.add_handler(CallbackQueryHandler(cb_goal_nudge, pattern=r"^gn:"))
    app.add_handler(CallbackQueryHandler(cb_quickadd, pattern=r"^qa:"))
    app.add_handler(CallbackQueryHandler(cb_editnote, pattern=r"^note:"))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.REPLY, on_edit_reply))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.REPLY, on_text))
    log.info("Expense bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
