from __future__ import annotations
from dataclasses import dataclass, field

from app.config import TIGER_ID, TIGER_ACCOUNT, TIGER_PRIVATE_KEY_PATH
from app.money import dollars_to_cents


class NotConfigured(RuntimeError):
    """Raised when Tiger credentials are absent. Callers turn this into a
    hidden tab, never a stack trace."""


@dataclass(frozen=True)
class Position:
    symbol: str
    name: str
    currency: str
    quantity: float
    avg_cost_cents: int
    market_price_cents: int
    market_value_cents: int
    unrealized_pl_cents: int
    account: str = ""


@dataclass(frozen=True)
class CashBalance:
    currency: str
    cash_cents: int


@dataclass(frozen=True)
class Portfolio:
    positions: list = field(default_factory=list)
    cash: list = field(default_factory=list)


def configured() -> bool:
    return bool(TIGER_ID and TIGER_ACCOUNT)


def _attr(obj, *names, default=None):
    """First present attribute wins."""
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _cents(value) -> int:
    """SDK returns dollar floats (confirmed in
    a live probe of TradeClient.get_positions'
    own docstring example, e.g. average_cost: 129.45) — never minor units."""
    if value is None:
        return 0
    return dollars_to_cents(value)


def _position(sdk_pos) -> Position:
    # A live probe of the SDK confirmed
    # tigeropen.trade.domain.position.Position via inspect.getsource: `account`
    # and `contract` are flat attributes, `average_cost`/`market_price`/
    # `market_value`/`unrealized_pnl` are the real field names (no aliases) —
    # so the candidate lists below are narrowed to just those confirmed names.
    contract = _attr(sdk_pos, "contract", default=sdk_pos)
    symbol = _attr(contract, "symbol", default="")
    return Position(
        symbol=str(symbol),
        name=str(_attr(contract, "name", default="") or symbol),
        currency=str(_attr(contract, "currency", default="USD")).upper(),
        quantity=float(_attr(sdk_pos, "quantity", default=0) or 0),
        avg_cost_cents=_cents(_attr(sdk_pos, "average_cost")),
        market_price_cents=_cents(_attr(sdk_pos, "market_price")),
        market_value_cents=_cents(_attr(sdk_pos, "market_value")),
        unrealized_pl_cents=_cents(_attr(sdk_pos, "unrealized_pnl")),
        account=str(_attr(sdk_pos, "account", default="") or ""),
    )


def build_client():  # pragma: no cover - requires real credentials
    from tigeropen.tiger_open_config import TigerOpenClientConfig
    from tigeropen.common.util.signature_utils import read_private_key
    from tigeropen.trade.trade_client import TradeClient

    config = TigerOpenClientConfig()
    config.private_key = read_private_key(TIGER_PRIVATE_KEY_PATH)
    config.tiger_id = TIGER_ID
    config.account = TIGER_ACCOUNT
    return TradeClient(config)


def fetch_portfolio(client=None) -> Portfolio:
    """Blocking. Never call from an async context without a threadpool."""
    if client is None:
        if not configured():
            raise NotConfigured("TIGER_ID and TIGER_ACCOUNT are required")
        client = build_client()

    positions = [_position(p) for p in (client.get_positions() or [])]

    cash = []
    for asset in (client.get_assets() or []):
        # Task 1's probe found TradeClient.get_assets() returns a list of
        # PortfolioAccount objects, which do NOT expose currency/cash flatly —
        # those live one level down on the `.summary` Account object
        # (confirmed against the live SDK).
        summary = _attr(asset, "summary", default=asset)
        currency = _attr(summary, "currency")
        if currency is None:
            continue
        cash.append(CashBalance(currency=str(currency).upper(),
                                cash_cents=_cents(_attr(summary, "cash"))))
    return Portfolio(positions=positions, cash=cash)


def _selfcheck() -> None:  # pragma: no cover - manual, needs real credentials
    """python -m app.tiger --selfcheck — prints a live fetch once creds exist."""
    portfolio = fetch_portfolio()
    print(f"positions: {len(portfolio.positions)}")
    for p in portfolio.positions:
        print(f"  {p.symbol:<8} {p.currency} qty={p.quantity} "
              f"value={p.market_value_cents / 100:,.2f} "
              f"pl={p.unrealized_pl_cents / 100:,.2f}")
    for c in portfolio.cash:
        print(f"  cash {c.currency} {c.cash_cents / 100:,.2f}")


if __name__ == "__main__":  # pragma: no cover
    import sys
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        print("usage: python -m app.tiger --selfcheck")
