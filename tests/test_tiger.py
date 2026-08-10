import pytest
from app import tiger


class FakeSdkPosition:
    """Mimics the SDK's position object using the attribute names recorded in
"""

    def __init__(self, symbol, currency="USD", quantity=10.0, avg_cost=90.0,
                 market_price=95.0, market_value=950.0, unrealized_pnl=50.0,
                 name=""):
        self.contract = type("C", (), {
            "symbol": symbol, "currency": currency, "name": name or symbol})()
        self.quantity = quantity
        self.average_cost = avg_cost
        self.market_price = market_price
        self.market_value = market_value
        self.unrealized_pnl = unrealized_pnl
        self.account = "U123"


class FakeSummary:
    """Mimics tigeropen.trade.domain.account.Account, the object living at
    PortfolioAccount.summary — this is where currency/cash actually live."""

    def __init__(self, currency="USD", cash=500.0):
        self.currency = currency
        self.cash = cash


class FakeAsset:
    """Mimics tigeropen.trade.domain.account.PortfolioAccount: no flat
    currency/cash, only a nested `.summary`."""

    def __init__(self, currency="USD", cash=500.0):
        self.summary = FakeSummary(currency, cash)


class FakeFlatAsset:
    """Has currency/cash directly on itself, no `.summary` attribute at all —
    exercises the `_attr(asset, "summary", default=asset)` fallback path."""

    def __init__(self, currency="SGD", cash=200.0):
        self.currency = currency
        self.cash = cash


class FakeClient:
    def __init__(self, positions=None, raises=None, assets=None):
        self._positions = positions or []
        self._raises = raises
        self._assets = assets or []

    def get_positions(self):
        if self._raises:
            raise self._raises
        return self._positions

    def get_assets(self):
        return self._assets


def test_fetch_maps_positions_to_integer_cents():
    p = tiger.fetch_portfolio(FakeClient([FakeSdkPosition("AAPL")]))
    assert len(p.positions) == 1
    pos = p.positions[0]
    assert pos.symbol == "AAPL"
    assert pos.currency == "USD"
    assert pos.quantity == 10.0
    assert pos.avg_cost_cents == 9000
    assert pos.market_price_cents == 9500
    assert pos.market_value_cents == 95000
    assert pos.unrealized_pl_cents == 5000


def test_negative_pl_keeps_its_sign():
    sdk = FakeSdkPosition("TSLA", unrealized_pnl=-123.45)
    pos = tiger.fetch_portfolio(FakeClient([sdk])).positions[0]
    assert pos.unrealized_pl_cents == -12345


def test_fractional_quantity_survives():
    sdk = FakeSdkPosition("VOO", quantity=2.75)
    pos = tiger.fetch_portfolio(FakeClient([sdk])).positions[0]
    assert pos.quantity == 2.75


def test_empty_portfolio_is_valid_not_an_error():
    p = tiger.fetch_portfolio(FakeClient([]))
    assert p.positions == []


def test_missing_optional_fields_default_to_zero():
    sdk = FakeSdkPosition("X")
    sdk.unrealized_pnl = None
    pos = tiger.fetch_portfolio(FakeClient([sdk])).positions[0]
    assert pos.unrealized_pl_cents == 0


def test_sdk_errors_propagate_unchanged():
    with pytest.raises(RuntimeError):
        tiger.fetch_portfolio(FakeClient(raises=RuntimeError("boom")))


def test_not_configured_raises_when_no_client_and_no_creds(monkeypatch):
    monkeypatch.setattr(tiger, "TIGER_ID", "")
    monkeypatch.setattr(tiger, "TIGER_ACCOUNT", "")
    assert tiger.configured() is False
    with pytest.raises(tiger.NotConfigured):
        tiger.fetch_portfolio()


def test_configured_requires_both_id_and_account(monkeypatch):
    monkeypatch.setattr(tiger, "TIGER_ID", "abc")
    monkeypatch.setattr(tiger, "TIGER_ACCOUNT", "")
    assert tiger.configured() is False
    monkeypatch.setattr(tiger, "TIGER_ACCOUNT", "U123")
    assert tiger.configured() is True


def test_cash_is_mapped_to_cash_balance_with_correct_cents():
    p = tiger.fetch_portfolio(FakeClient(assets=[FakeAsset(currency="USD", cash=500.0)]))
    assert len(p.cash) == 1
    assert p.cash[0].currency == "USD"
    assert p.cash[0].cash_cents == 50000


def test_cash_currency_is_upper_cased():
    p = tiger.fetch_portfolio(FakeClient(assets=[FakeAsset(currency="usd", cash=100.0)]))
    assert p.cash[0].currency == "USD"


def test_asset_with_no_currency_is_skipped_not_crashed():
    p = tiger.fetch_portfolio(FakeClient(assets=[FakeAsset(currency=None, cash=100.0)]))
    assert p.cash == []


def test_flat_asset_without_summary_still_works_via_fallback():
    # No `.summary` attribute at all — `_attr(asset, "summary", default=asset)`
    # must fall back to reading currency/cash off the asset itself.
    p = tiger.fetch_portfolio(FakeClient(assets=[FakeFlatAsset(currency="sgd", cash=200.0)]))
    assert len(p.cash) == 1
    assert p.cash[0].currency == "SGD"
    assert p.cash[0].cash_cents == 20000
