# expense-tracker

A self-hosted personal finance tracker for Singapore: a **Telegram bot** captures
spending the moment it happens, **bank/merchant emails are auto-imported**, and a
**mobile-first web dashboard** (PWA) does the analysis. Two small containers, one
SQLite file, no cloud services.

> It replaces a commercial expense app with something that ingests your actual
> bank alerts and answers on my own hardware.

![Web dashboard](image/dashboard.png)
*The dashboard in dark mode — home, insights, budgets and merchant views (personal amounts redacted).*

UI heavily inspired by [Cashew](https://github.com/jameskokoska/Cashew) by
James Kokoska — a lovely open-source budget app; this project began life as a
self-hosted replacement for it (see the CSV importer below).

## Highlights

- **Frictionless capture** — text the bot `kopi 2.80` and it's logged, categorised
  by keyword rules, with inline buttons to fix category or edit the note. Bare
  `/add` offers quick-add buttons based on your own frequent spends (median price).
- **Email auto-import** — polls Gmail over IMAP and parses transaction alerts from
  Trust Bank, Grab e-receipts, DBS PayLah!, DBS iBanking (PayNow), FairPrice Group
  app and ShopBack Pay. Anything else that *looks* like a receipt gets a
  suggest-and-confirm Telegram ping instead of silent auto-import — new merchants
  need zero code to at least surface.
- **Merchant memory** — correct the category on an imported charge once and the
  app remembers that *brand*, so its next charge lands right. Outlet names and
  per-transaction reference codes are stripped, so one correction covers every
  branch.
- **Cards** — a second grouping dimension beside category: which card paid. Each
  parser extracts whatever instrument the sender prints; corrections teach the
  attribution. Filter the whole Home view by card.
- **Dedup that understands app payments** — a Grab order charged to a bank card
  fires *two* emails; the merchant receipt wins, the bank alert is skipped.
- **Reimbursements** — incoming PayNow can be linked back to the original expense
  as a negative row in its category, so monthly sums net out automatically.
- **Budgets, goals, recurring** — per-category budgets with pace alerts
  (projection > 110% after day 10), savings goals as allocations (never
  double-counted as transactions) with optional auto-funding and a wishlist,
  recurring rules back-filled after downtime.
- **Insights dashboard** — month-over-month comparison, merchant breakdown,
  spending heatmap, cashflow, year view with per-category drill-down, weekly
  rhythm — Material You-styled vanilla JS + Chart.js, light/dark, installable PWA.
- **Portfolio** (optional) — read-only Tiger Brokers holdings with an toggle
  and history, cache-first so the dashboard never waits on the broker API.
- **Payslip archive** (optional) — if your payroll provider mails you a
  password-protected payslip PDF, it is decrypted, OCR'd, archived and broken
  down in a Pay tab.
- **Login** — session-cookie auth on the dashboard, stdlib `scrypt` hashing,
  throttled logins.
- **Daily digest & month-end report** — 23:59 SGT summary of everything recorded
  today; a report card with goal nudge on the 1st.

## What's new since the first public release

The initial release (July 2026) covered capture, email ingestion, budgets,
recurring rules and the insights dashboard. Everything below landed after it.

**Login (web dashboard).** The dashboard is no longer open to anyone on the LAN.
A session cookie gates every route except the login page, its assets, `/health`
and `/sw.js`. Passwords use stdlib `scrypt` with the work factor stored inside
the hash string, so it can be raised later without invalidating anyone. Sessions
are absolute 90 days — never extended by use, enforced server-side. Failed
logins are throttled per-IP *and* per-username, with a dummy verify for unknown
usernames so response timing doesn't reveal which accounts exist. Data is still
shared between accounts; per-account isolation is not implemented.

**Merchant memory.** Correcting the category of an email-imported transaction
teaches the app that merchant. The bank's merchant string is reduced to a brand
stem (`NTUC FP-ORCHARD SINGAPORE SG` → `ntuc fp`) so every outlet is covered by
one correction, while per-transaction reference codes are ignored. Precedence is
**trip > memory > keyword rules**. Learning is go-forward only and only rows from
the six known parsers teach — typed notes and unknown-sender subjects never do.
`/merchants` lists what's been learned, with buttons to forget.

**Cards.** Which card paid, as a first-class dimension. `effective_card` resolves
**memory → typed rule → sender default → unassigned**; a correction you made
outranks a rule you typed once. The binding key is `(source, instrument)`, so one
physical card can learn as several instruments when senders print it differently
(a Grab ride shows last-4, a GrabFood order shows the brand) without them
colliding. Deleting a card archives it when transactions reference it. Home has a
card filter that scopes the donut, month total, sparkline, heatmap and list.

**Portfolio (optional, Tiger Brokers).** Read-only holdings, totals, an 
toggle and a history chart. Cache-first: the bot polls on a timer and writes to
SQLite; the dashboard only reads that cache, with a manual refresh button that
runs the same fetch. The  rate is stored *per snapshot*, so history reflects
the rate at the time. Leaving `TIGER_ID` empty disables the feature entirely —
hidden tab, no outbound calls.

**Payslip archive (optional).** If your payroll provider mails a
password-protected payslip PDF, the same Gmail poll archives it. Matched on
sender + a PDF attachment, never on subject. The pay month is read from inside
the document, because a payslip commonly arrives in the month *after* the one it
covers. Line items are stored generically as `(section, label, amount)` in
document order rather than a fixed gross/CPF schema, so an unexpected bonus or
new allowance is preserved rather than dropped. No income transaction is ever
written — the bank credit alert already covers that. Empty
`PAYSLIP_PDF_PASSWORD`/`PAYSLIP_SENDER` disables the whole feature.

**Goals wishlist.** Goals gained target dates, monthly auto-funding, categories,
priorities and a bought-marker. Pace is *derived on every read*, never stored,
which is what lets it self-correct after a skipped month. Auto-funding rides the
existing daily recurring tick with a forward-only cursor, so it is idempotent and
back-fills after downtime.

**Desktop layout.** At ≥1100px the dashboard is a multi-column tabbed app rather
than one long scroll; Transactions and Budgets dissolve into Home's grid. One
markup tree serves both shapes. The active tab lives in `location.hash`, so tabs
are bookmarkable.

**Accessibility.** Clickable rows are reachable by Tab and fire on Enter/Space
via a single delegated handler. Dialogs set `inert` on the rest of the app and
un-inert *before* restoring focus. Charts carry `role="img"` with generated
labels; the toast is an `aria-live` region.

**Travel mode, CSV export, PWA polish** and a **Cashew CSV importer** for
one-time history backfill.

## Architecture

```
┌─────────────┐   long-poll   ┌──────────────┐
│  Telegram   │◄─────────────►│ expense-bot  │──┐  APScheduler:
└─────────────┘               │ (PTB v21)    │  │  recurring + goals 00:10,
                              └──────┬───────┘  │  digest 23:59, IMAP poll
┌─────────────┐    IMAP              │          │  every 15 min, portfolio poll
│   Gmail     │◄─────────────────────┘          ▼
└─────────────┘                          ┌────────────┐
┌─────────────┐    HTTP    ┌──────────┐  │ SQLite DB  │
│  Browser /  │◄──────────►│expense-web│◄┤ (one file, │
│  PWA        │            │ (FastAPI) │  │ WAL mode)  │
└─────────────┘            └──────────┘  └────────────┘
```

- **Two services, one image** — the bot owns all scheduled side-effects; the web
  container only reads/writes the shared DB. Money is stored as **integer cents**.
- **Email ingestion is go-forward only** — first run seeds a high-water IMAP-UID
  cursor and never backfills, so historical overlap with imported data is impossible.
- **No JS framework, no build step** — vanilla JS SPA; Chart.js and fonts are
  fetched **at image build time** (no runtime CDN dependency).
- **Migrations are additive only** — boot runs `CREATE TABLE IF NOT EXISTS` and
  `ALTER TABLE ADD COLUMN`, never `DROP`.

## Quick start

Prereqs: Docker + Compose, a Telegram bot token from [@BotFather](https://t.me/BotFather),
your numeric chat ID from [@userinfobot](https://t.me/userinfobot).

```bash
cp .env.example .env        # set TELEGRAM_BOT_TOKEN + ALLOWED_CHAT_ID
docker compose up -d --build
```

The SQLite DB lands in `./data/` (bind mount — repoint the `volumes:` entries in
`compose.yaml` if you want it elsewhere).

Web dashboard: `http://<host>:8085`. On first visit no accounts exist, so
`/signup` is open — create yours and signup closes behind you. To add someone
later, set `SIGNUP_CODE` in `.env` and recreate the stack. The bot answers
**only** `ALLOWED_CHAT_ID` and silently ignores everyone else.

> ⚠️ **The threat model is LAN privacy only.** The login cookie deliberately does
> not set `Secure`, because this is served over plain HTTP on a home network —
> which also means passwords cross the LAN in cleartext. Put it behind HTTPS
> before exposing it anywhere beyond your own network.

Lost the password? There is no reset flow — delete the row and sign up again:
`sqlite3 ./data/expenses.db "DELETE FROM users;"`

### Optional: email auto-import

Enable Google 2-Step Verification, create an **App Password**, then set
`GMAIL_USER` + `GMAIL_APP_PASSWORD` in `.env` and recreate the stack
(compose env changes need stop→start, not restart). Leave the password empty
to disable email polling entirely.

### Optional: portfolio and payslips

Both are off unless configured, and both are gated on a single empty-string
check — no tab, no polling, no outbound calls until you fill them in. See the
`TIGER_*` and `PAYSLIP_*` blocks in `.env.example`.

## Bot commands

| Command | Does |
|---|---|
| `kopi 2.80` (plain text) | quick-add with keyword categorisation |
| `/add [amount] [note]` | add; bare `/add` shows quick-add buttons |
| `/undo`, `/recent [n]`, `/find <text>` | fix-ups and lookup |
| `/summary [YYYY-MM]` | month summary |
| `/budget [Category amount]` | view / set budgets |
| `/income 5000 july salary` | record income |
| `/recurring add 1200 monthly 1 rent` | recurring rules (daily/weekly/monthly) |
| `/goal add Japan 3000` · `/goal put Japan 200` | savings goals |
| `/merchants` | learned merchants, most-used first, with 🗑 to forget |
| `/cards` | each card, its month spend, and which senders feed it |
| `/payslip [n]` | one-shot backfill of payslips from recent payroll mail |
| `/trip on\|off` | travel mode — route all new spends to Travel |

Every transaction message the bot sends carries ✏️ buttons (fix category / edit
note / assign card) via stateless ForceReply — they survive bot restarts.

## Email parsers

Parsing lives in `app/trust_email.py` — pure functions, no Telegram/DB imports,
unit-tested in `tests/test_trust_email.py` against anonymised copies of the real
HTML bodies (whitespace runs, entities and field order preserved). Currently:

| Sender | Imports |
|---|---|
| Trust Bank | card spends (local + overseas), outgoing PayNow; cancellations warn |
| Grab | food/mart orders (exact receipt time) and rides |
| DBS PayLah! | outgoing wallet spends; incoming money → suggest-as-income |
| DBS iBanking | outgoing account PayNow; incoming transfers → suggest-as-income |
| FairPrice app | app-payment receipts |
| ShopBack Pay | merchant + payment date; cashback notices ignored |
| *anyone else* | receipt-shaped emails → ✅ Add / ❌ Ignore suggestion |

Each parser also extracts the **instrument** the sender prints — `Trust Link
card`, `A/C ending 1234`, `Apple Pay (**** 4242)`, a bare last-4, or a wallet
name — which is what the card attribution is keyed on. Senders differ in what
they reveal: a Grab ride names only the last 4 digits because the brand lives in
an `<img alt>`, and ShopBack names the wallet rather than the card behind it.

## Tests

```bash
pip install -r requirements-dev.txt
pytest        # 724 tests: parsers, money math, DB, bot handlers, web API, layout
```

`tests/test_static_layout.py` asserts structural invariants of the HTML/JS (tab
counts, hash routing, filter wiring). It renders nothing — layout changes still
need checking by eye at both widths.

## One-time history import

`import_cashew.py` loads a [Cashew](https://cashewapp.web.app/) CSV export
(`--dry-run` first, `--selftest` for the row transform), so email ingestion can
stay go-forward with no overlap.

## Conventions & limitations

- Single-household by design — one chat ID, one Gmail account. The web login
  authenticates, but all accounts see the same data.
- Categories + alert thresholds: `app/config.py`; keyword rules: `app/categories.py`.
- The dashboard greeting name is per-device:
  `localStorage.setItem("expense.name", "Your Name")` in the browser console.
- Parsers are written against the email formats of one set of Singapore banks and
  merchants. They will need adjusting for yours — that's what the test fixtures
  in `tests/test_trust_email.py` are for.
