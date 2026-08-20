from types import SimpleNamespace

import pytest
from ib_async import AccountValue
from ib_async.contract import Option, Stock

from thetagang.accounting import (
    AccountingError,
    AccountMetric,
    BrokerAccountSnapshot,
    CapitalBaseKind,
    CashLedger,
    PortfolioAccounting,
    PositionCategory,
    RegimeRebalanceBaseEnum,
    account_summary_from_values,
    select_account_value,
)


def _config(
    *,
    account_margin: float = 1.2,
    wheel_margin: float | None = None,
    regime_margin: float | None = None,
    weight_base: RegimeRebalanceBaseEnum = (RegimeRebalanceBaseEnum.net_liq_ex_options),
):
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            account=SimpleNamespace(margin_usage=account_margin),
        ),
        portfolio=SimpleNamespace(symbols={"AAA": object(), "BBB": object()}),
        strategies=SimpleNamespace(
            cash_management=SimpleNamespace(cash_fund="SHV"),
            regime_rebalance=SimpleNamespace(
                symbols=["AAA", "BBB"],
                weight_base=weight_base,
            ),
        ),
    )
    config.wheel_margin_usage = lambda: (
        account_margin if wheel_margin is None else wheel_margin
    )
    config.regime_margin_usage = lambda: (
        account_margin if regime_margin is None else regime_margin
    )
    return config


def _summary(*, net_liquidation: float = 100_000, cash: float = 5_000):
    return {
        AccountMetric.NET_LIQUIDATION.value: SimpleNamespace(value=net_liquidation),
        AccountMetric.TOTAL_CASH.value: SimpleNamespace(value=cash),
    }


def _position(contract, quantity: float, market_value: float):
    return SimpleNamespace(
        contract=contract,
        position=quantity,
        marketValue=market_value,
    )


def test_account_summary_prefers_aggregate_base_values() -> None:
    values = [
        AccountValue("A", "TotalCashValue", "900", "USD", ""),
        AccountValue("A", "TotalCashValue", "1000", "BASE", "MODEL"),
        AccountValue("A", "TotalCashValue", "1100", "BASE", ""),
    ]

    summary = account_summary_from_values(values)

    assert summary["TotalCashValue"].value == "1100"
    assert select_account_value(values, AccountMetric.TOTAL_CASH) == 1100


def test_account_value_selection_fails_closed_on_ambiguous_aggregate() -> None:
    values = [
        AccountValue("A", "NetLiquidation", "1000", "BASE", ""),
        AccountValue("A", "NetLiquidation", "2000", "BASE", ""),
    ]

    assert "NetLiquidation" not in account_summary_from_values(values)
    with pytest.raises(AccountingError, match="unavailable"):
        select_account_value(values, AccountMetric.NET_LIQUIDATION)


def test_capital_bases_apply_their_own_margin_policy() -> None:
    accounting = PortfolioAccounting.build(
        config=_config(account_margin=1.2, wheel_margin=0.5, regime_margin=1.4),
        account_summary=_summary(),
    )

    assert accounting.capital_base(CapitalBaseKind.NET_LIQUIDATION).value == 100_000
    assert accounting.capital_base(CapitalBaseKind.WHEEL_BUYING_POWER).value == 50_000
    regime = accounting.capital_base(
        CapitalBaseKind.REGIME_REBALANCE,
        market_prices={"AAA": 100.0, "BBB": 100.0},
    )
    assert regime.value == 140_000
    assert regime.margin_usage == 1.4


def test_position_taxonomy_splits_state_owned_and_manual_option_value() -> None:
    tail_put = Option("CCC", "20270115", 100.0, "P", "SMART")
    tail_put.conId = 801
    positions = {"CCC": [_position(tail_put, 2, 10_000)]}
    accounting = PortfolioAccounting.build(
        config=_config(),
        account_summary=_summary(),
        portfolio_positions=positions,
        tail_owned_quantities={801: 1},
    )

    assert accounting.positions.value(PositionCategory.TAIL_HEDGE_OPTION) == 5_000
    assert accounting.positions.value(PositionCategory.OTHER_OPTION) == 5_000
    base = accounting.capital_base(
        CapitalBaseKind.REGIME_REBALANCE,
        market_prices={"AAA": 100.0, "BBB": 100.0},
    )
    assert base.excluded_value == 5_000
    assert base.value == 114_000


def test_active_regime_option_is_excluded_in_addition_to_owned_tail_value() -> None:
    tail_put = Option("AAA", "20270115", 100.0, "P", "SMART")
    tail_put.conId = 801
    positions = {"AAA": [_position(tail_put, 2, 10_000)]}
    accounting = PortfolioAccounting.build(
        config=_config(),
        account_summary=_summary(),
        portfolio_positions=positions,
        tail_owned_quantities={801: 1},
    )

    base = accounting.capital_base(
        CapitalBaseKind.REGIME_REBALANCE,
        market_prices={"AAA": 100.0, "BBB": 100.0},
        tail_hedge_value_override=4_000,
    )

    assert accounting.positions.value(PositionCategory.TAIL_HEDGE_OPTION) == 5_000
    assert accounting.positions.value(PositionCategory.REGIME_OPTION) == 5_000
    assert base.excluded_value == 9_000
    assert base.value == 109_200


def test_managed_stock_base_does_not_apply_margin_usage() -> None:
    positions = {
        "AAA": [_position(Stock("AAA", "SMART", "USD"), 4, 410)],
        "BBB": [_position(Stock("BBB", "SMART", "USD"), 3, 290)],
    }
    accounting = PortfolioAccounting.build(
        config=_config(weight_base=RegimeRebalanceBaseEnum.managed_stocks),
        account_summary=_summary(),
        portfolio_positions=positions,
    )

    base = accounting.capital_base(
        CapitalBaseKind.REGIME_REBALANCE,
        market_prices={"AAA": 100.0, "BBB": 100.0},
    )

    assert base.value == 700
    assert base.margin_usage == 1


def test_cash_ledger_never_uses_pending_credit_to_fund_a_sweep_buy() -> None:
    ledger = CashLedger(
        settled_cash=1_500,
        pending_debit=200,
        pending_credit=10_000,
        reserved_cash=300,
    )

    assert ledger.after_pending_debits == 1_300
    assert ledger.sweepable_cash == 1_000
    assert ledger.projected_cash == 11_300
    assert (
        ledger.amount_to_sweep(
            target_cash=1_000,
            buy_threshold=100,
            sell_threshold=100,
        )
        == 0
    )


def test_cash_ledger_fails_closed_when_pending_orders_are_ambiguous() -> None:
    ledger = CashLedger(settled_cash=1_000, ambiguous=True)

    with pytest.raises(AccountingError, match="cannot be priced safely"):
        ledger.amount_to_sweep(
            target_cash=1_000,
            buy_threshold=100,
            sell_threshold=100,
        )


def test_account_snapshot_rejects_nonpositive_net_liquidation() -> None:
    snapshot = BrokerAccountSnapshot(_summary(net_liquidation=0))

    with pytest.raises(AccountingError, match="unavailable"):
        _ = snapshot.net_liquidation
