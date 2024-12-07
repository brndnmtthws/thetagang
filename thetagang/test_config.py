from polyfactory.factories import DataclassFactory

from thetagang.config import (
    AccountConfig,
    Config,
    OptionChainsConfig,
    RollWhenConfig,
    SymbolConfig,
    TargetConfig,
)


class TargetConfigFactory(DataclassFactory[TargetConfig]): ...


class TargetConfigPutsFactory(DataclassFactory[TargetConfig.Puts]): ...


class TargetConfigCallsFactory(DataclassFactory[TargetConfig.Puts]): ...


class RollWhenConfigFactory(DataclassFactory[RollWhenConfig]): ...


class OptionChainsConfigFactory(DataclassFactory[OptionChainsConfig]): ...


class AccountConfigFactory(DataclassFactory[AccountConfig]): ...


class SymbolConfigFactory(DataclassFactory[SymbolConfig]): ...


class SymbolConfigPutsFactory(DataclassFactory[SymbolConfig.Puts]): ...


class SymbolConfigCallsFactory(DataclassFactory[SymbolConfig.Calls]): ...


class ConfigFactory(DataclassFactory[Config]): ...


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
