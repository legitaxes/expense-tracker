from __future__ import annotations
import json
import time
import urllib.request

from app import db

# One hour. FX moves slowly enough that a portfolio view does not need better,
# and the endpoint is a free public service worth being polite to.
CACHE_TTL_SECONDS = 3600
_ENDPOINT = "https://api.frankfurter.app/latest?from={base}&to=SGD"
_TIMEOUT = 10
# frankfurter 403s the default urllib UA (Python-urllib/3.x). An honest,
# descriptive bot UA is the convention here (an honest UA on a sibling end-
# matrix) and is not spoofing a
# browser — verified 200 from this host with this exact string.
_USER_AGENT = "expense-tracker/1.0 (homelab; self-hosted)"


def _http_fetch(currency: str) -> float:
    """Fetch server-side. A browser fetch() to frankfurter from the dashboard
    origin fails with net::ERR_FAILED (documented in CLAUDE.md for the
    Homepage forex widget) — the request must originate from this process."""
    url = _ENDPOINT.format(base=currency)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return float(payload["rates"]["SGD"])


def sgd_rate(conn, currency: str, fetcher=None, now=None) -> float | None:
    """SGD per 1 unit of `currency`, or None if no rate can be obtained.

    Never raises. A currency conversion failing must not be able to take down
    the portfolio page, so every failure path degrades to cache-or-None.
    """
    currency = (currency or "").upper()
    if not currency:
        return None
    if currency == "SGD":
        return 1.0

    fetcher = fetcher or _http_fetch
    now = time.time() if now is None else now
    key, at_key = f"fx_rate_{currency}", f"fx_rate_{currency}_at"

    cached = db.get_setting(conn, key)
    cached_at = db.get_setting(conn, at_key)
    if cached is not None and cached_at is not None:
        try:
            if now - float(cached_at) < CACHE_TTL_SECONDS:
                return float(cached)
        except (TypeError, ValueError):
            cached = None  # corrupt kv row — treat as a cache miss

    try:
        rate = float(fetcher(currency))
    except Exception:
        # Stale-but-real beats nothing: the toggle keeps working through an
        # outage, and the page shows the rate's age.
        try:
            return float(cached) if cached is not None else None
        except (TypeError, ValueError):
            return None

    db.set_setting(conn, key, rate)
    db.set_setting(conn, at_key, now)
    return rate
