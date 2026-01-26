from polyfactory.factories.pydantic_factory import ModelFactory

from thetagang.config import (
    AccountConfig,
    Config,
    OptionChainsConfig,
    RegimeRebalanceConfig,
    RollWhenConfig,
    SymbolConfig,
    TargetConfig,
)


class TargetConfigFactory(ModelFactory[TargetConfig]): ...


class TargetConfigPutsFactory(ModelFactory[TargetConfig.Puts]): ...


class TargetConfigCallsFactory(ModelFactory[TargetConfig.Calls]): ...


class RollWhenConfigFactory(ModelFactory[RollWhenConfig]): ...


class OptionChainsConfigFactory(ModelFactory[OptionChainsConfig]): ...


class AccountConfigFactory(ModelFactory[AccountConfig]): ...


class SymbolConfigFactory(ModelFactory[SymbolConfig]): ...


class SymbolConfigPutsFactory(ModelFactory[SymbolConfig.Puts]): ...


class SymbolConfigCallsFactory(ModelFactory[SymbolConfig.Calls]): ...


class RegimeRebalanceConfigFactory(ModelFactory[RegimeRebalanceConfig]):
    soft_band = 0.10
    hard_band = 0.50
    flow_trade_min = 2000.0
    flow_trade_stop = 1000.0
    flow_imbalance_tau = 0.70
    deficit_rail_start = 5000.0
    deficit_rail_stop = 2500.0
    ratio_gate = None


class ConfigFactory(ModelFactory[Config]):
    @classmethod
    def build(cls, factory_use_construct: bool = False, **kwargs):
        kwargs.setdefault(
            "regime_rebalance",
            RegimeRebalanceConfigFactory.build(soft_band=0.10, hard_band=0.50),
        )
        return super().build(factory_use_construct=factory_use_construct, **kwargs)


def test_trading_is_allowed_with_symbol_no_trading() -> None:
    config = ConfigFactory.build(
        symbols={"AAPL": SymbolConfigFactory.build(no_trading=True, weight=1.0)},
    )
    assert not config.trading_is_allowed("AAPL")


def test_trading_is_allowed_with_symbol_trading_allowed() -> None:
    config = ConfigFactory.build(
        symbols={"AAPL": SymbolConfigFactory.build(no_trading=False, weight=1.0)},
    )
    assert config.trading_is_allowed("AAPL")


def test_is_buy_only_rebalancing_when_true() -> None:
    config = ConfigFactory.build(
        symbols={
            "AAPL": SymbolConfigFactory.build(buy_only_rebalancing=True, weight=1.0)
        },
    )
    assert config.is_buy_only_rebalancing("AAPL")


def test_is_buy_only_rebalancing_when_false() -> None:
    config = ConfigFactory.build(
        symbols={
            "AAPL": SymbolConfigFactory.build(buy_only_rebalancing=False, weight=1.0)
        },
    )
    assert not config.is_buy_only_rebalancing("AAPL")


def test_is_buy_only_rebalancing_when_none() -> None:
    config = ConfigFactory.build(
        symbols={
            "AAPL": SymbolConfigFactory.build(buy_only_rebalancing=None, weight=1.0)
        },
    )
    assert not config.is_buy_only_rebalancing("AAPL")


def test_is_buy_only_rebalancing_for_missing_symbol() -> None:
    config = ConfigFactory.build(
        symbols={"AAPL": SymbolConfigFactory.build(weight=1.0)},
    )
    assert not config.is_buy_only_rebalancing("MSFT")
