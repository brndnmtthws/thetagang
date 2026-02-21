import math
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, Field, model_validator
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree
from typing_extensions import Self

from thetagang.config_models import (
    AccountConfig,
    ActionWhenClosedEnum,
    CashManagementConfig,
    ConstantsConfig,
    DatabaseConfig,
    DisplayMixin,
    ExchangeHoursConfig,
    IBAsyncConfig,
    IBCConfig,
    OptionChainsConfig,
    OrdersConfig,
    RatioGateConfig,
    RegimeRebalanceBaseEnum,
    RegimeRebalanceConfig,
    RollWhenConfig,
    SymbolConfig,
    TargetConfig,
    VIXCallHedgeConfig,
    WatchdogConfig,
    WriteWhenConfig,
    error_console,
)
from thetagang.fmt import dfmt, ffmt, pfmt

__all__ = [
    "AccountConfig",
    "ActionWhenClosedEnum",
    "CashManagementConfig",
    "ConstantsConfig",
    "DatabaseConfig",
    "DisplayMixin",
    "ExchangeHoursConfig",
    "IBAsyncConfig",
    "IBCConfig",
    "LegacyConfig",
    "OptionChainsConfig",
    "OrdersConfig",
    "RatioGateConfig",
    "RegimeRebalanceBaseEnum",
    "RegimeRebalanceConfig",
    "RollWhenConfig",
    "SymbolConfig",
    "TargetConfig",
    "VIXCallHedgeConfig",
    "WatchdogConfig",
    "WriteWhenConfig",
    "normalize_config",
]


class LegacyConfig(BaseModel, DisplayMixin):
    account: AccountConfig
    option_chains: OptionChainsConfig
    roll_when: RollWhenConfig
    target: TargetConfig
    exchange_hours: ExchangeHoursConfig = Field(default_factory=ExchangeHoursConfig)

    orders: OrdersConfig = Field(default_factory=OrdersConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    ib_async: IBAsyncConfig = Field(default_factory=IBAsyncConfig)
    ibc: IBCConfig = Field(default_factory=IBCConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    cash_management: CashManagementConfig = Field(default_factory=CashManagementConfig)
    vix_call_hedge: VIXCallHedgeConfig = Field(default_factory=VIXCallHedgeConfig)
    write_when: WriteWhenConfig = Field(default_factory=WriteWhenConfig)
    symbols: Dict[str, SymbolConfig] = Field(default_factory=dict)
    constants: ConstantsConfig = Field(default_factory=ConstantsConfig)
    regime_rebalance: RegimeRebalanceConfig = Field(
        default_factory=RegimeRebalanceConfig
    )

    def trading_is_allowed(self, symbol: str) -> bool:
        symbol_config = self.symbols.get(symbol)
        return not symbol_config or not symbol_config.no_trading

    def is_buy_only_rebalancing(self, symbol: str) -> bool:
        symbol_config = self.symbols.get(symbol)
        return symbol_config is not None and symbol_config.buy_only_rebalancing is True

    def is_sell_only_rebalancing(self, symbol: str) -> bool:
        symbol_config = self.symbols.get(symbol)
        return symbol_config is not None and symbol_config.sell_only_rebalancing is True

    def is_regime_rebalance_symbol(self, symbol: str) -> bool:
        return self.regime_rebalance.enabled and symbol in self.regime_rebalance.symbols

    def symbol_config(self, symbol: str) -> Optional[SymbolConfig]:
        return self.symbols.get(symbol)

    @model_validator(mode="after")
    def check_symbols(self) -> Self:
        if not self.symbols:
            raise ValueError("At least one symbol must be specified")
        return self

    @model_validator(mode="after")
    def check_symbol_weights(self) -> Self:
        if not math.isclose(
            1, sum([s.weight or 0.0 for s in self.symbols.values()]), rel_tol=1e-5
        ):
            raise ValueError("Symbol weights must sum to 1.0")
        return self

    def get_target_delta(self, symbol: str, right: str) -> float:
        p_or_c = "calls" if right.upper().startswith("C") else "puts"
        symbol_config = self.symbols.get(symbol)

        if symbol_config:
            option_config = getattr(symbol_config, p_or_c, None)
            if option_config and option_config.delta is not None:
                return option_config.delta
            if symbol_config.delta is not None:
                return symbol_config.delta

        target_option = getattr(self.target, p_or_c, None)
        if target_option and target_option.delta is not None:
            return target_option.delta

        return self.target.delta

    def maintain_high_water_mark(self, symbol: str) -> bool:
        symbol_config = self.symbols.get(symbol)
        if (
            symbol_config
            and symbol_config.calls
            and symbol_config.calls.maintain_high_water_mark is not None
        ):
            return symbol_config.calls.maintain_high_water_mark
        return self.roll_when.calls.maintain_high_water_mark

    def get_write_threshold_sigma(
        self,
        symbol: str,
        right: str,
    ) -> Optional[float]:
        p_or_c = "calls" if right.upper().startswith("C") else "puts"
        symbol_config = self.symbols.get(symbol)

        if symbol_config:
            option_config = getattr(symbol_config, p_or_c, None)
            if option_config:
                if option_config.write_threshold_sigma is not None:
                    return option_config.write_threshold_sigma
                if option_config.write_threshold is not None:
                    return None

            if symbol_config.write_threshold_sigma is not None:
                return symbol_config.write_threshold_sigma
            if symbol_config.write_threshold is not None:
                return None

        option_constants = getattr(self.constants, p_or_c, None)
        if option_constants and option_constants.write_threshold_sigma is not None:
            return option_constants.write_threshold_sigma
        if self.constants.write_threshold_sigma is not None:
            return self.constants.write_threshold_sigma

        return None

    def get_write_threshold_perc(
        self,
        symbol: str,
        right: str,
    ) -> float:
        p_or_c = "calls" if right.upper().startswith("C") else "puts"
        symbol_config = self.symbols.get(symbol)

        if symbol_config:
            option_config = getattr(symbol_config, p_or_c, None)
            if option_config and option_config.write_threshold is not None:
                return option_config.write_threshold
            if symbol_config.write_threshold is not None:
                return symbol_config.write_threshold

        option_constants = getattr(self.constants, p_or_c, None)
        if option_constants and option_constants.write_threshold is not None:
            return option_constants.write_threshold
        if self.constants.write_threshold is not None:
            return self.constants.write_threshold

        return 0.0

    def create_symbols_table(self) -> Table:
        table = Table(
            title="Configured symbols and target weights",
            box=box.SIMPLE_HEAVY,
            show_lines=True,
        )
        table.add_column("Symbol")
        table.add_column("Weight", justify="right")
        table.add_column("Buy-only", justify="center")
        table.add_column("Sell-only", justify="center")
        table.add_column("Call delta", justify="right")
        table.add_column("Call strike limit", justify="right")
        table.add_column("Call threshold", justify="right")
        table.add_column("HWM", justify="right")
        table.add_column("Put delta", justify="right")
        table.add_column("Put strike limit", justify="right")
        table.add_column("Put threshold", justify="right")

        for symbol, sconfig in self.symbols.items():
            call_thresh = (
                f"{ffmt(self.get_write_threshold_sigma(symbol, 'C'))}σ"
                if self.get_write_threshold_sigma(symbol, "C")
                else pfmt(self.get_write_threshold_perc(symbol, "C"))
            )
            put_thresh = (
                f"{ffmt(self.get_write_threshold_sigma(symbol, 'P'))}σ"
                if self.get_write_threshold_sigma(symbol, "P")
                else pfmt(self.get_write_threshold_perc(symbol, "P"))
            )

            table.add_row(
                symbol,
                pfmt(sconfig.weight or 0.0),
                "✓" if sconfig.buy_only_rebalancing else "",
                "✓" if sconfig.sell_only_rebalancing else "",
                ffmt(self.get_target_delta(symbol, "C")),
                dfmt(sconfig.calls.strike_limit if sconfig.calls else None),
                call_thresh,
                str(self.maintain_high_water_mark(symbol)),
                ffmt(self.get_target_delta(symbol, "P")),
                dfmt(sconfig.puts.strike_limit if sconfig.puts else None),
                put_thresh,
            )
        return table

    def display(self, config_path: str) -> None:
        console = Console()
        config_table = Table(box=box.SIMPLE_HEAVY)
        config_table.add_column("Section")
        config_table.add_column("Setting")
        config_table.add_column("")
        config_table.add_column("Value")

        # Add all component tables
        self.account.add_to_table(config_table)
        self.exchange_hours.add_to_table(config_table)
        if self.constants:
            self.constants.add_to_table(config_table)
        self.orders.add_to_table(config_table)
        self.database.add_to_table(config_table)
        self.roll_when.add_to_table(config_table)
        self.write_when.add_to_table(config_table)
        self.target.add_to_table(config_table)
        self.cash_management.add_to_table(config_table)
        self.vix_call_hedge.add_to_table(config_table)
        self.regime_rebalance.add_to_table(config_table)

        # Create tree and add tables
        tree = Tree(":control_knobs:")
        tree.add(Group(f":file_cabinet: Loaded from {config_path}", config_table))
        tree.add(Group(":yin_yang: Symbology", self.create_symbols_table()))

        console.print(Panel(tree, title="Config"))

    def get_target_dte(self, symbol: str) -> int:
        symbol_config = self.symbols.get(symbol)
        return (
            symbol_config.dte
            if symbol_config and symbol_config.dte is not None
            else self.target.dte
        )

    def get_cap_factor(self, symbol: str) -> float:
        symbol_config = self.symbols.get(symbol)
        if (
            symbol_config is not None
            and symbol_config.calls is not None
            and symbol_config.calls.cap_factor is not None
        ):
            return symbol_config.calls.cap_factor
        return self.write_when.calls.cap_factor

    def get_cap_target_floor(self, symbol: str) -> float:
        symbol_config = self.symbols.get(symbol)
        if (
            symbol_config is not None
            and symbol_config.calls is not None
            and symbol_config.calls.cap_target_floor is not None
        ):
            return symbol_config.calls.cap_target_floor
        return self.write_when.calls.cap_target_floor

    def get_strike_limit(self, symbol: str, right: str) -> Optional[float]:
        p_or_c = "calls" if right.upper().startswith("C") else "puts"
        symbol_config = self.symbols.get(symbol)
        option_config = getattr(symbol_config, p_or_c, None) if symbol_config else None
        return option_config.strike_limit if option_config else None

    def write_excess_calls_only(self, symbol: str) -> bool:
        symbol_config = self.symbols.get(symbol)
        if (
            symbol_config is not None
            and symbol_config.calls is not None
            and symbol_config.calls.excess_only is not None
        ):
            return symbol_config.calls.excess_only
        return self.write_when.calls.excess_only

    def get_max_dte_for(self, symbol: str) -> Optional[int]:
        if symbol == "VIX" and self.vix_call_hedge.max_dte is not None:
            return self.vix_call_hedge.max_dte
        symbol_config = self.symbols.get(symbol)
        if symbol_config is not None and symbol_config.max_dte is not None:
            return symbol_config.max_dte
        return self.target.max_dte

    def can_write_when(self, symbol: str, right: str) -> Tuple[bool, bool]:
        symbol_config = self.symbols.get(symbol)
        p_or_c = "calls" if right.upper().startswith("C") else "puts"
        option_config = (
            getattr(symbol_config, p_or_c, None) if symbol_config is not None else None
        )
        default_config = getattr(self.write_when, p_or_c)
        can_write_when_green = (
            option_config.write_when.green
            if option_config is not None
            and option_config.write_when is not None
            and option_config.write_when.green is not None
            else default_config.green
        )
        can_write_when_red = (
            option_config.write_when.red
            if option_config is not None
            and option_config.write_when is not None
            and option_config.write_when.red is not None
            else default_config.red
        )
        return (can_write_when_green, can_write_when_red)

    def close_if_unable_to_roll(self, symbol: str) -> bool:
        symbol_config = self.symbols.get(symbol)
        return (
            symbol_config.close_if_unable_to_roll
            if symbol_config is not None
            and symbol_config.close_if_unable_to_roll is not None
            else self.roll_when.close_if_unable_to_roll
        )


def normalize_config(config: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    # Do any pre-processing necessary to the config here, such as handling
    # defaults, deprecated values, config changes, etc.
    if "minimum_cushion" in config["account"]:
        raise RuntimeError(
            "Config error: minimum_cushion is deprecated and replaced with margin_usage. See sample config for details."
        )

    if "ib_insync" in config:
        error_console.print(
            "WARNING: config param `ib_insync` is deprecated, please rename it to the equivalent `ib_async`.",
        )

        if "ib_async" not in config:
            # swap the old ib_insync key to the new ib_async key
            config["ib_async"] = config["ib_insync"]
        del config["ib_insync"]

    ibc_config = config.get("ibc")
    if isinstance(ibc_config, dict) and "twsVersion" in ibc_config:
        error_console.print(
            "WARNING: config param ibc.twsVersion is deprecated, please remove it from your config.",
        )

        # TWS version is pinned to latest stable, delete any existing config if it's present
        del ibc_config["twsVersion"]

    if "maximum_new_contracts" in config["target"]:
        error_console.print(
            "WARNING: config param target.maximum_new_contracts is deprecated, please remove it from your config.",
        )

        del config["target"]["maximum_new_contracts"]

    # xor: should have weight OR parts, but not both
    if any(["weight" in s for s in config["symbols"].values()]) == any(
        ["parts" in s for s in config["symbols"].values()]
    ):
        raise RuntimeError(
            "ERROR: all symbols should have either a weight or parts specified, but parts and weights cannot be mixed."
        )

    if "parts" in list(config["symbols"].values())[0]:
        # If using "parts" instead of "weight", convert parts into weights
        total_parts = float(sum([s["parts"] for s in config["symbols"].values()]))
        for k in config["symbols"].keys():
            config["symbols"][k]["weight"] = config["symbols"][k]["parts"] / total_parts
        for s in config["symbols"].values():
            del s["parts"]

    if (
        "close_at_pnl" in config["roll_when"]
        and config["roll_when"]["close_at_pnl"]
        and config["roll_when"]["close_at_pnl"] <= config["roll_when"]["min_pnl"]
    ):
        raise RuntimeError(
            "ERROR: roll_when.close_at_pnl needs to be greater than roll_when.min_pnl."
        )

    return config
