from __future__ import annotations

import math
from collections import defaultdict
from enum import Enum
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from thetagang.config_models import (
    AccountConfig,
    CashManagementConfig,
    ConstantsConfig,
    DatabaseConfig,
    DisplayMixin,
    ExchangeHoursConfig,
    IBAsyncConfig,
    IBCConfig,
    OptionChainsConfig,
    OrdersConfig,
    RegimeRebalanceConfig,
    RollWhenConfig,
    SymbolConfig,
    TargetConfig,
    VIXCallHedgeConfig,
    WatchdogConfig,
    WriteWhenConfig,
)
from thetagang.fmt import dfmt, ffmt, pfmt

STAGE_KIND_BY_ID: dict[str, str] = {
    "options_write_puts": "options.write_puts",
    "options_write_calls": "options.write_calls",
    "equity_regime_rebalance": "equity.regime_rebalance",
    "equity_buy_rebalance": "equity.buy_rebalance",
    "equity_sell_rebalance": "equity.sell_rebalance",
    "options_roll_positions": "options.roll_positions",
    "options_close_positions": "options.close_positions",
    "post_vix_call_hedge": "post.vix_call_hedge",
    "post_cash_management": "post.cash_management",
}

CANONICAL_STAGE_ORDER: list[str] = [
    "options_write_puts",
    "options_write_calls",
    "equity_regime_rebalance",
    "equity_buy_rebalance",
    "equity_sell_rebalance",
    "options_roll_positions",
    "options_close_positions",
    "post_vix_call_hedge",
    "post_cash_management",
]

WHEEL_OPTION_STAGE_IDS = {
    "options_write_puts",
    "options_write_calls",
    "options_roll_positions",
    "options_close_positions",
}

RUN_STRATEGY_IDS = {
    "wheel",
    "regime_rebalance",
    "vix_call_hedge",
    "cash_management",
}

STRATEGY_STAGE_IDS: dict[str, set[str]] = {
    "wheel": {
        "options_write_puts",
        "options_write_calls",
        "equity_buy_rebalance",
        "equity_sell_rebalance",
        "options_roll_positions",
        "options_close_positions",
    },
    "regime_rebalance": {"equity_regime_rebalance"},
    "vix_call_hedge": {"post_vix_call_hedge"},
    "cash_management": {"post_cash_management"},
}

WHEEL_SYMBOL_OVERRIDE_KEYS = [
    "write_calls_only_min_threshold_percent",
    "write_calls_only_min_threshold_percent_relative",
]

EXPLICIT_STAGE_PREREQUISITES: dict[str, set[str]] = {
    # Call writing relies on target share quantities computed in put-write planning.
    "options_write_calls": {"options_write_puts"},
}


class ConfigMeta(BaseModel):
    schema_version: int = Field(2)

    @model_validator(mode="after")
    def validate_schema_version(self) -> "ConfigMeta":
        if self.schema_version != 2:
            raise ValueError("meta.schema_version must be 2")
        return self


class RunStageConfig(BaseModel):
    id: str
    kind: str
    enabled: bool = True
    depends_on: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_stage_identity(self) -> "RunStageConfig":
        expected_kind = STAGE_KIND_BY_ID.get(self.id)
        if expected_kind is None:
            raise ValueError(f"run.stages contains unknown stage id: {self.id}")
        if self.kind != expected_kind:
            raise ValueError(
                f"run.stages.{self.id}.kind must be {expected_kind}, got {self.kind}"
            )
        return self


class RunConfig(BaseModel):
    stages: List[RunStageConfig] = Field(default_factory=list)
    strategies: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_stage_ids(self) -> "RunConfig":
        if not self.stages and not self.strategies:
            raise ValueError(
                "run must define at least one of run.strategies or run.stages"
            )
        if self.stages and self.strategies:
            raise ValueError(
                "run must define exactly one of run.strategies or run.stages, not both"
            )

        if self.strategies:
            unknown = [s for s in self.strategies if s not in RUN_STRATEGY_IDS]
            if unknown:
                raise ValueError(
                    f"run.strategies contains unknown strategy id(s): {', '.join(unknown)}"
                )
            if len(set(self.strategies)) != len(self.strategies):
                raise ValueError("run.strategies must not contain duplicates")
            if "wheel" in self.strategies and "regime_rebalance" in self.strategies:
                raise ValueError(
                    "run.strategies cannot enable wheel and regime_rebalance together"
                )
            return self

        stage_ids = [s.id for s in self.stages]
        if len(set(stage_ids)) != len(stage_ids):
            raise ValueError("run.stages ids must be unique")
        seen = set(stage_ids)
        index_by_id = {stage.id: idx for idx, stage in enumerate(self.stages)}
        for stage in self.stages:
            for dep in stage.depends_on:
                if dep not in seen:
                    raise ValueError(
                        f"run.stages.{stage.id} depends_on unknown stage {dep}"
                    )
                if index_by_id[dep] >= index_by_id[stage.id]:
                    raise ValueError(
                        f"run.stages.{stage.id} depends_on {dep} must appear earlier in run.stages order"
                    )

        enabled_by_id = {stage.id: stage.enabled for stage in self.stages}
        for stage in self.stages:
            if stage.enabled and any(
                not enabled_by_id[dep] for dep in stage.depends_on
            ):
                raise ValueError(
                    f"run.stages.{stage.id} is enabled but depends on a disabled stage"
                )

        graph: dict[str, list[str]] = defaultdict(list)
        for stage in self.stages:
            graph[stage.id].extend(stage.depends_on)
        visiting: set[str] = set()
        visited: set[str] = set()

        def dfs(node: str) -> None:
            if node in visiting:
                raise ValueError(
                    f"run.stages contains a dependency cycle involving {node}"
                )
            if node in visited:
                return
            visiting.add(node)
            for dep in graph[node]:
                dfs(dep)
            visiting.remove(node)
            visited.add(node)

        for stage_id in graph:
            dfs(stage_id)

        enabled_stage_ids = {stage.id for stage in self.stages if stage.enabled}
        for stage_id, required_ids in EXPLICIT_STAGE_PREREQUISITES.items():
            if stage_id not in enabled_stage_ids:
                continue
            missing = sorted(required_ids - enabled_stage_ids)
            if missing:
                missing_text = ", ".join(missing)
                raise ValueError(
                    f"run.stages.{stage_id} requires enabled stage(s): {missing_text}"
                )
        if "equity_regime_rebalance" in enabled_stage_ids and (
            enabled_stage_ids & WHEEL_OPTION_STAGE_IDS
        ):
            raise ValueError(
                "run.stages cannot enable equity_regime_rebalance together with "
                "wheel options stages (options_write_*/options_roll_positions/options_close_positions)"
            )
        return self

    def resolved_stages(self) -> List[RunStageConfig]:
        if self.stages:
            return list(self.stages)

        enabled: set[str] = set()
        for strategy_id in self.strategies:
            enabled.update(STRATEGY_STAGE_IDS[strategy_id])

        ordered_ids = [
            stage_id for stage_id in CANONICAL_STAGE_ORDER if stage_id in enabled
        ]
        resolved: List[RunStageConfig] = []
        prev: Optional[str] = None
        for stage_id in ordered_ids:
            deps: List[str] = [prev] if prev else []
            resolved.append(
                RunStageConfig(
                    id=stage_id,
                    kind=STAGE_KIND_BY_ID[stage_id],
                    enabled=True,
                    depends_on=deps,
                )
            )
            prev = stage_id
        return resolved


class RuntimeConfig(BaseModel):
    account: AccountConfig
    option_chains: OptionChainsConfig
    exchange_hours: ExchangeHoursConfig = Field(default_factory=ExchangeHoursConfig)
    orders: OrdersConfig = Field(default_factory=OrdersConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    ib_async: IBAsyncConfig = Field(default_factory=IBAsyncConfig)
    ibc: IBCConfig = Field(default_factory=IBCConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)


class PortfolioConfig(BaseModel):
    symbols: Dict[str, SymbolConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_symbols(self) -> "PortfolioConfig":
        if not self.symbols:
            raise ValueError("At least one symbol must be specified")
        return self

    @model_validator(mode="after")
    def check_symbol_weights(self) -> "PortfolioConfig":
        if not math.isclose(
            1, sum([s.weight or 0.0 for s in self.symbols.values()]), rel_tol=1e-5
        ):
            raise ValueError("Symbol weights must sum to 1.0")
        return self


class RebalanceMode(str, Enum):
    off = "off"
    buy_only = "buy_only"
    sell_only = "sell_only"
    both = "both"


class RebalanceExecutionPolicy(BaseModel):
    mode: RebalanceMode = RebalanceMode.off
    min_threshold_shares: Optional[int] = Field(default=None, ge=1)
    min_threshold_amount: Optional[float] = Field(default=None, ge=0.0)
    min_threshold_percent: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    min_threshold_percent_relative: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )

    def allows_buy(self) -> bool:
        return self.mode in {RebalanceMode.buy_only, RebalanceMode.both}

    def allows_sell(self) -> bool:
        return self.mode in {RebalanceMode.sell_only, RebalanceMode.both}


class RebalanceExecutionPolicyOverride(BaseModel):
    mode: Optional[RebalanceMode] = None
    min_threshold_shares: Optional[int] = Field(default=None, ge=1)
    min_threshold_amount: Optional[float] = Field(default=None, ge=0.0)
    min_threshold_percent: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    min_threshold_percent_relative: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )

    def apply_to(self, base: RebalanceExecutionPolicy) -> RebalanceExecutionPolicy:
        return RebalanceExecutionPolicy(
            mode=self.mode if self.mode is not None else base.mode,
            min_threshold_shares=(
                self.min_threshold_shares
                if self.min_threshold_shares is not None
                else base.min_threshold_shares
            ),
            min_threshold_amount=(
                self.min_threshold_amount
                if self.min_threshold_amount is not None
                else base.min_threshold_amount
            ),
            min_threshold_percent=(
                self.min_threshold_percent
                if self.min_threshold_percent is not None
                else base.min_threshold_percent
            ),
            min_threshold_percent_relative=(
                self.min_threshold_percent_relative
                if self.min_threshold_percent_relative is not None
                else base.min_threshold_percent_relative
            ),
        )


class RebalanceExecutionConfig(BaseModel):
    defaults: RebalanceExecutionPolicyOverride = Field(
        default_factory=RebalanceExecutionPolicyOverride
    )
    symbol_overrides: Dict[str, RebalanceExecutionPolicyOverride] = Field(
        default_factory=dict
    )

    def resolve(
        self, symbol: str, *, fallback_mode: RebalanceMode
    ) -> RebalanceExecutionPolicy:
        base = self.defaults.apply_to(RebalanceExecutionPolicy(mode=fallback_mode))
        symbol_override = self.symbol_overrides.get(symbol)
        if symbol_override is None:
            return base
        return symbol_override.apply_to(base)


class StrategyRiskConfig(BaseModel):
    margin_usage: Optional[float] = Field(default=None, ge=0.0)


class WheelDefaultsConfig(BaseModel):
    target: TargetConfig
    write_when: WriteWhenConfig = Field(default_factory=WriteWhenConfig)
    roll_when: RollWhenConfig
    constants: ConstantsConfig = Field(default_factory=ConstantsConfig)
    write_calls_only_min_threshold_percent: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )
    write_calls_only_min_threshold_percent_relative: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )


class WheelSymbolOverrideConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    write_calls_only_min_threshold_percent: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )
    write_calls_only_min_threshold_percent_relative: Optional[float] = Field(
        default=None, ge=0.0, le=1.0
    )


class WheelStrategyConfig(BaseModel):
    defaults: WheelDefaultsConfig
    symbol_overrides: Dict[str, WheelSymbolOverrideConfig] = Field(default_factory=dict)
    risk: StrategyRiskConfig = Field(default_factory=StrategyRiskConfig)
    equity_rebalance: RebalanceExecutionConfig = Field(
        default_factory=RebalanceExecutionConfig
    )


class RegimeRebalanceStrategyConfig(RegimeRebalanceConfig):
    risk: StrategyRiskConfig = Field(default_factory=StrategyRiskConfig)
    equity_rebalance: RebalanceExecutionConfig = Field(
        default_factory=lambda: RebalanceExecutionConfig(
            defaults=RebalanceExecutionPolicyOverride(mode=RebalanceMode.both)
        )
    )


class StrategiesConfig(BaseModel):
    wheel: WheelStrategyConfig
    regime_rebalance: RegimeRebalanceStrategyConfig = Field(
        default_factory=RegimeRebalanceStrategyConfig
    )
    vix_call_hedge: VIXCallHedgeConfig = Field(default_factory=VIXCallHedgeConfig)
    cash_management: CashManagementConfig = Field(default_factory=CashManagementConfig)


class Config(BaseModel, DisplayMixin):
    model_config = ConfigDict(extra="forbid")

    meta: ConfigMeta = Field(default_factory=ConfigMeta)
    run: RunConfig
    runtime: RuntimeConfig
    portfolio: PortfolioConfig
    strategies: StrategiesConfig

    @model_validator(mode="after")
    def apply_strategy_overrides(self) -> "Config":
        symbols = self.portfolio.symbols

        def apply_wheel_symbol_overrides(
            strategy_overrides: Dict[str, WheelSymbolOverrideConfig], keys: List[str]
        ) -> None:
            for symbol, overrides in strategy_overrides.items():
                symbol_cfg = symbols.get(symbol)
                if symbol_cfg is None:
                    continue
                for key in keys:
                    value = getattr(overrides, key)
                    if value is not None:
                        setattr(symbol_cfg, key, value)

        wheel_defaults = self.strategies.wheel.defaults
        if (
            wheel_defaults.write_calls_only_min_threshold_percent is not None
            and self.strategies.wheel.defaults.write_when.calls.min_threshold_percent
            is None
        ):
            self.strategies.wheel.defaults.write_when.calls.min_threshold_percent = (
                wheel_defaults.write_calls_only_min_threshold_percent
            )
        if (
            wheel_defaults.write_calls_only_min_threshold_percent_relative is not None
            and self.strategies.wheel.defaults.write_when.calls.min_threshold_percent_relative
            is None
        ):
            self.strategies.wheel.defaults.write_when.calls.min_threshold_percent_relative = wheel_defaults.write_calls_only_min_threshold_percent_relative

        apply_wheel_symbol_overrides(
            self.strategies.wheel.symbol_overrides,
            WHEEL_SYMBOL_OVERRIDE_KEYS,
        )

        return self

    @property
    def account(self) -> AccountConfig:
        return self.runtime.account

    @property
    def option_chains(self) -> OptionChainsConfig:
        return self.runtime.option_chains

    @property
    def exchange_hours(self) -> ExchangeHoursConfig:
        return self.runtime.exchange_hours

    @property
    def orders(self) -> OrdersConfig:
        return self.runtime.orders

    @property
    def database(self) -> DatabaseConfig:
        return self.runtime.database

    @property
    def ib_async(self) -> IBAsyncConfig:
        return self.runtime.ib_async

    @property
    def ibc(self) -> IBCConfig:
        return self.runtime.ibc

    @property
    def watchdog(self) -> WatchdogConfig:
        return self.runtime.watchdog

    @property
    def symbols(self) -> Dict[str, SymbolConfig]:
        return self.portfolio.symbols

    @property
    def target(self) -> TargetConfig:
        return self.strategies.wheel.defaults.target

    @property
    def write_when(self) -> WriteWhenConfig:
        return self.strategies.wheel.defaults.write_when

    @property
    def roll_when(self) -> RollWhenConfig:
        return self.strategies.wheel.defaults.roll_when

    @property
    def constants(self) -> ConstantsConfig:
        return self.strategies.wheel.defaults.constants

    @property
    def cash_management(self) -> CashManagementConfig:
        return self.strategies.cash_management

    @property
    def vix_call_hedge(self) -> VIXCallHedgeConfig:
        return self.strategies.vix_call_hedge

    @property
    def regime_rebalance(self) -> RegimeRebalanceStrategyConfig:
        return self.strategies.regime_rebalance

    def wheel_rebalance_policy(self, symbol: str) -> RebalanceExecutionPolicy:
        return self.strategies.wheel.equity_rebalance.resolve(
            symbol, fallback_mode=RebalanceMode.off
        )

    def regime_rebalance_policy(self, symbol: str) -> RebalanceExecutionPolicy:
        return self.strategies.regime_rebalance.equity_rebalance.resolve(
            symbol, fallback_mode=RebalanceMode.both
        )

    def wheel_margin_usage(self) -> float:
        strategy_value = self.strategies.wheel.risk.margin_usage
        if strategy_value is not None:
            return strategy_value
        return self.runtime.account.margin_usage

    def regime_margin_usage(self) -> float:
        strategy_value = self.strategies.regime_rebalance.risk.margin_usage
        if strategy_value is not None:
            return strategy_value
        return self.runtime.account.margin_usage

    def trading_is_allowed(self, symbol: str) -> bool:
        symbol_config = self.symbols.get(symbol)
        return not symbol_config or not symbol_config.no_trading

    def is_buy_only_rebalancing(self, symbol: str) -> bool:
        policy = self.wheel_rebalance_policy(symbol)
        return policy.mode in {RebalanceMode.buy_only, RebalanceMode.both}

    def is_sell_only_rebalancing(self, symbol: str) -> bool:
        policy = self.wheel_rebalance_policy(symbol)
        return policy.mode in {RebalanceMode.sell_only, RebalanceMode.both}

    def is_regime_rebalance_symbol(self, symbol: str) -> bool:
        return self.regime_rebalance.enabled and symbol in self.regime_rebalance.symbols

    def symbol_config(self, symbol: str) -> Optional[SymbolConfig]:
        return self.symbols.get(symbol)

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

    def get_write_threshold_sigma(self, symbol: str, right: str) -> Optional[float]:
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

    def get_write_threshold_perc(self, symbol: str, right: str) -> float:
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
                "✓" if self.wheel_rebalance_policy(symbol).allows_buy() else "",
                "✓" if self.wheel_rebalance_policy(symbol).allows_sell() else "",
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


DEFAULT_RUN_STRATEGIES: list[str] = ["wheel", "vix_call_hedge", "cash_management"]


def enabled_stage_ids_from_run(run: RunConfig) -> List[str]:
    return [stage.id for stage in run.resolved_stages() if stage.enabled]


def stage_enabled_map(config: Config) -> Dict[str, bool]:
    return stage_enabled_map_from_run(config.run)


def stage_enabled_map_from_run(run: RunConfig) -> Dict[str, bool]:
    resolved_ids = set(enabled_stage_ids_from_run(run))
    return {stage_id: (stage_id in resolved_ids) for stage_id in STAGE_KIND_BY_ID}
