from polyfactory.factories.pydantic_factory import ModelFactory

from thetagang.config import (
    AccountConfig,
    Config,
    OptionChainsConfig,
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


class ConfigFactory(ModelFactory[Config]): ...


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
