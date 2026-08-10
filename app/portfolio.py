from __future__ import annotations
from datetime import date, datetime

from app import db, fx, tiger


def _row(position) -> dict:
    return {"symbol": position.symbol, "name": position.name,
            "currency": position.currency, "quantity": position.quantity,
            "avg_cost_cents": position.avg_cost_cents,
            "market_price_cents": position.market_price_cents,
            "market_value_cents": position.market_value_cents,
            "unrealized_pl_cents": position.unrealized_pl_cents,
            "account": position.account}


def store(conn, portfolio, rates, today=None) -> None:
    """Persist a fetched portfolio: holdings wholesale, history upserted."""
    today = today or date.today().isoformat()
    db.replace_positions(conn, [_row(p) for p in portfolio.positions])

    cash_by_ccy = {c.currency: c.cash_cents for c in portfolio.cash}
    grouped = {}
    for p in portfolio.positions:
        agg = grouped.setdefault(p.currency, {"value": 0, "pl": 0})
        agg["value"] += p.market_value_cents
        agg["pl"] += p.unrealized_pl_cents
    for currency in cash_by_ccy:
        grouped.setdefault(currency, {"value": 0, "pl": 0})

    for currency, agg in grouped.items():
        db.upsert_snapshot(conn, today, currency, agg["value"],
                           cash_by_ccy.get(currency, 0), agg["pl"],
                           rates.get(currency))

    db.set_setting(conn, "portfolio_last_poll", datetime.now().isoformat(timespec="seconds"))
    db.set_setting(conn, "portfolio_last_error", "")


def _totals(rows, rates) -> list:
    """Computed from live positions, never from snapshots — one source per
    question, so current and historical state can never disagree."""
    grouped = {}
    for r in rows:
        agg = grouped.setdefault(r["currency"], {"value": 0, "cost": 0, "pl": 0})
        agg["value"] += r["market_value_cents"]
        agg["cost"] += round(r["avg_cost_cents"] * r["quantity"])
        agg["pl"] += r["unrealized_pl_cents"]
    return [{"currency": ccy, "value_cents": a["value"],
             "cost_cents": a["cost"], "pl_cents": a["pl"],
             "sgd_rate": rates.get(ccy)}
            for ccy, a in sorted(grouped.items(), key=lambda kv: -kv[1]["value"])]


def _cached_rates(conn, currencies) -> dict:
    """Read-path rates come from the cache only — a GET must never block on
    an outbound HTTP call."""
    rates = {}
    for ccy in currencies:
        if ccy == "SGD":
            rates[ccy] = 1.0
            continue
        cached = db.get_setting(conn, f"fx_rate_{ccy}")
        try:
            rates[ccy] = float(cached) if cached is not None else None
        except (TypeError, ValueError):
            rates[ccy] = None
    return rates


def current(conn) -> dict:
    rows = db.list_positions(conn)
    rates = _cached_rates(conn, {r["currency"] for r in rows})
    last_poll = db.get_setting(conn, "portfolio_last_poll")

    stale_minutes = None
    if last_poll:
        try:
            delta = datetime.now() - datetime.fromisoformat(last_poll)
            stale_minutes = int(delta.total_seconds() // 60)
        except ValueError:
            stale_minutes = None

    return {
        "positions": [{k: r[k] for k in r.keys()} for r in rows],
        "totals": _totals(rows, rates),
        "last_poll": last_poll,
        "last_error": db.get_setting(conn, "portfolio_last_error", "") or "",
        "stale_minutes": stale_minutes,
    }


def history(conn, days: int = 90) -> dict:
    rows = db.snapshot_history(conn, days)
    return {"points": [{"date": r["snapshot_date"], "currency": r["currency"],
                        "value_cents": r["total_value_cents"],
                        "cash_cents": r["cash_cents"],
                        "pl_cents": r["unrealized_pl_cents"],
                        "sgd_rate": r["sgd_rate"]} for r in rows]}


def refresh(conn, client=None) -> dict:
    """Blocking. Callers on an event loop must wrap this in a threadpool."""
    try:
        fetched = tiger.fetch_portfolio(client)
    except Exception as exc:
        db.set_setting(conn, "portfolio_last_error", f"{type(exc).__name__}: {exc}")
        raise
    currencies = {p.currency for p in fetched.positions}
    currencies.update(c.currency for c in fetched.cash)
    rates = {ccy: fx.sgd_rate(conn, ccy) for ccy in currencies}
    store(conn, fetched, rates)
    return current(conn)
