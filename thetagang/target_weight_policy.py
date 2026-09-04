from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from thetagang.accounting import AccountingError, AccountMetric, BrokerAccountSnapshot
from thetagang.config_models import TargetWeightPolicyConfig
from thetagang.external_decisions import (
    DecisionInput,
    ExternalDecisionError,
    ExternalDecisionMarketData,
    ExternalDecisionRequest,
    ExternalDecisionRequestEnvelope,
    ExternalDecisionResponse,
    ExternalDecisionResponseEnvelope,
    build_external_decision_request,
    validate_market_decision_response,
)

if TYPE_CHECKING:
    from thetagang.config import Config

TARGET_WEIGHT_DECISION_TYPE = "regime_target_weights"
TARGET_WEIGHT_POLICY_STATE_EVENT = "target_weight_policy_state"


class VolatilityWeightInput(DecisionInput):
    enabled: bool
    target_vol: float
    lookback_days: int
    min_weight: float
    max_weight: float
    rebalance_band: float
    smoothing_factor: float
    increase_smoothing_factor: float | None
    decrease_smoothing_factor: float | None


class VolatilityContext(DecisionInput):
    config: VolatilityWeightInput | None
    calculation: dict[str, float] | None = Field(
        description="Host calculation diagnostics; absent when unavailable."
    )


class AbsoluteTrendInput(DecisionInput):
    enabled: bool
    lookback_days: int
    risk_off_multiplier: float


class ExecutionConstraints(DecisionInput):
    trading_allowed: bool
    rebalance_mode: Literal["both", "buy_only", "sell_only", "off"]
    min_threshold_shares: int | None
    min_threshold_amount: float | None
    min_threshold_percent: float | None
    min_threshold_percent_relative: float | None


class TargetWeightSymbolInput(DecisionInput):
    configured_weight: float
    post_volatility_weight: float
    current_weight: float
    current_value: float
    current_shares: int
    market_price: float
    volatility_weight: VolatilityContext
    absolute_trend: AbsoluteTrendInput | None
    execution_constraints: ExecutionConstraints


class RatioGateInput(DecisionInput):
    enabled: bool
    anchor: str
    drift_max: float
    vol_min: float | None


class TargetWeightStrategyInput(DecisionInput):
    name: Literal["regime_rebalance"]
    weight_base: Literal["net_liq", "net_liq_ex_options"]
    margin_usage: float
    lookback_days: int
    soft_band: float
    hard_band: float
    hard_band_rebalance_fraction: float
    cooldown_days: int
    last_rebalance_at: AwareDatetime | None
    choppiness_min: float
    efficiency_max: float
    flow_trade_min: float
    flow_trade_stop: float
    flow_imbalance_tau: float
    deficit_rail_start: float
    deficit_rail_stop: float
    ratio_gate: RatioGateInput | None


class TargetWeightAccountInput(DecisionInput):
    metrics: dict[str, float] = Field(
        description="Available IBKR metrics; missing metrics are omitted, never zero-filled."
    )
    rebalance_base_value: float
    excluded_option_value: float


class TargetWeightPortfolioInput(DecisionInput):
    configured_total_weight: float
    post_volatility_total_weight: float


class MultiplierConstraints(DecisionInput):
    min_multiplier: float
    max_multiplier: float
    clamp_to_volatility_bounds: bool


class TotalWeightConstraint(DecisionInput):
    max_total_weight: float | None
    effective_max_total_weight: float
    default_prevents_additional_leverage: bool


class TargetWeightDecisionInput(DecisionInput):
    """USD values and fractional weights for the regime target multiplier hook."""

    strategy: TargetWeightStrategyInput
    account: TargetWeightAccountInput
    portfolio: TargetWeightPortfolioInput
    symbols: dict[str, TargetWeightSymbolInput]
    adjustment_constraints: dict[str, MultiplierConstraints]
    total_weight_constraint: TotalWeightConstraint
    market_data: ExternalDecisionMarketData


class TargetWeightDecisionRequest(ExternalDecisionRequestEnvelope):
    decision_type: Literal["regime_target_weights"]
    input: TargetWeightDecisionInput


class TargetWeightMultiplier(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    multiplier: float = Field(..., ge=0.0, strict=True)
    reason: str | None = None


class TargetWeightDecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adjustments: dict[str, TargetWeightMultiplier]


class TargetWeightDecisionResponse(ExternalDecisionResponseEnvelope):
    decision_type: Literal["regime_target_weights"]
    as_of_session: date
    output: TargetWeightDecisionOutput


def validate_target_weight_response(
    response: ExternalDecisionResponse,
    *,
    policy: TargetWeightPolicyConfig,
    history_dates: list[date],
    now: datetime,
) -> dict[str, TargetWeightMultiplier]:
    validate_market_decision_response(
        response,
        history_dates=history_dates,
        max_signal_age_sessions=policy.max_signal_age_sessions,
        now=now,
        decision_name="target weight policy",
    )

    try:
        output = TargetWeightDecisionOutput.model_validate(response.output)
    except ValueError as exc:
        raise ExternalDecisionError(
            "target weight policy returned invalid adjustments"
        ) from exc

    expected_symbols = set(policy.symbols)
    response_symbols = set(output.adjustments)
    if response_symbols != expected_symbols:
        missing = sorted(expected_symbols - response_symbols)
        unknown = sorted(response_symbols - expected_symbols)
        differences = []
        if missing:
            differences.append(f"missing={','.join(missing)}")
        if unknown:
            differences.append(f"unknown={','.join(unknown)}")
        raise ExternalDecisionError(
            "target weight policy response symbols do not match configuration"
            + (f" ({'; '.join(differences)})" if differences else "")
        )

    for symbol, adjustment in output.adjustments.items():
        multiplier = adjustment.multiplier
        limits = policy.symbols[symbol]
        if multiplier < limits.min_multiplier or multiplier > limits.max_multiplier:
            raise ExternalDecisionError(
                f"target weight policy multiplier for {symbol} is outside "
                "configured bounds"
            )
    return output.adjustments


def target_weight_policy_total_limit(
    effective_weights: dict[str, float], policy: TargetWeightPolicyConfig
) -> float:
    return max(sum(effective_weights.values()), policy.max_total_weight or 1.0)


def apply_target_weight_adjustments(
    effective_weights: dict[str, float],
    adjustments: dict[str, TargetWeightMultiplier],
    *,
    policy: TargetWeightPolicyConfig,
    volatility_bounds: dict[str, tuple[float, float]],
    eps: float,
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Apply the same host allocation limits during live planning and replay."""

    adjusted_weights = dict(effective_weights)
    details: dict[str, dict[str, float]] = {}
    for symbol, adjustment in adjustments.items():
        baseline_weight = effective_weights[symbol]
        raw_weight = baseline_weight * adjustment.multiplier
        if not math.isfinite(raw_weight) or raw_weight < 0:
            raise ExternalDecisionError(
                f"target weight policy produced an invalid weight for {symbol}"
            )
        effective_weight = raw_weight
        if policy.symbols[symbol].clamp_to_volatility_bounds:
            minimum, maximum = volatility_bounds[symbol]
            effective_weight = max(minimum, min(effective_weight, maximum))
        if effective_weight > 1:
            raise ExternalDecisionError(
                f"target weight policy produced an invalid weight for {symbol}"
            )
        adjusted_weights[symbol] = effective_weight
        details[symbol] = {
            "baseline_weight": baseline_weight,
            "raw_weight": raw_weight,
            "effective_weight": effective_weight,
        }
    if (
        sum(adjusted_weights.values())
        > target_weight_policy_total_limit(effective_weights, policy) + eps
    ):
        raise ExternalDecisionError(
            "target weight policy exceeds the permitted total weight"
        )
    return adjusted_weights, details


def build_target_weight_symbol_input(
    config: Config,
    symbol: str,
    *,
    symbol_config: Any,
    volatility_detail: dict[str, float] | None,
    effective_weight: float,
    total_value: float,
    current_position: int,
    current_value: float,
    market_price: float,
) -> dict[str, Any]:
    volatility_weight = getattr(symbol_config, "volatility_weight", None)
    volatility_config = None
    if volatility_weight is not None:
        volatility_config = VolatilityWeightInput.model_validate(
            volatility_weight, from_attributes=True
        ).model_dump(mode="json")

    absolute_trend = getattr(symbol_config, "absolute_trend", None)
    absolute_trend_config = None
    if absolute_trend is not None:
        absolute_trend_config = AbsoluteTrendInput.model_validate(
            absolute_trend, from_attributes=True
        ).model_dump(mode="json")

    rebalance_policy_fn = getattr(config, "regime_rebalance_policy", None)
    rebalance_policy = (
        rebalance_policy_fn(symbol) if callable(rebalance_policy_fn) else None
    )

    def resolved_threshold(name: str) -> Any:
        policy_value = getattr(rebalance_policy, name, None)
        if policy_value is not None:
            return policy_value
        suffix = name.removeprefix("min_threshold_")
        buy_value = getattr(
            symbol_config,
            f"buy_only_min_threshold_{suffix}",
            None,
        )
        if buy_value is not None:
            return buy_value
        return getattr(
            symbol_config,
            f"sell_only_min_threshold_{suffix}",
            None,
        )

    return {
        "configured_weight": float(symbol_config.weight),
        "post_volatility_weight": effective_weight,
        "current_weight": current_value / total_value,
        "current_value": current_value,
        "current_shares": current_position,
        "market_price": market_price,
        "volatility_weight": {
            "config": volatility_config,
            "calculation": volatility_detail,
        },
        "absolute_trend": absolute_trend_config,
        "execution_constraints": {
            "trading_allowed": bool(config.trading_is_allowed(symbol)),
            "rebalance_mode": (
                rebalance_policy.mode.value if rebalance_policy is not None else "both"
            ),
            "min_threshold_shares": resolved_threshold("min_threshold_shares"),
            "min_threshold_amount": resolved_threshold("min_threshold_amount"),
            "min_threshold_percent": resolved_threshold("min_threshold_percent"),
            "min_threshold_percent_relative": resolved_threshold(
                "min_threshold_percent_relative"
            ),
        },
    }


def build_target_weight_request(
    *,
    generated_at: datetime,
    dry_run: bool,
    config: Config,
    effective_weights: dict[str, float],
    symbols: list[str],
    symbol_configs: dict[str, Any],
    volatility_details: dict[str, dict[str, float]],
    account: BrokerAccountSnapshot,
    regime_margin_usage: float,
    total_value: float,
    excluded_value: float,
    last_rebalance: datetime | None,
    current_positions: dict[str, int],
    current_values: dict[str, float],
    market_prices: dict[str, float],
    market_data: ExternalDecisionMarketData,
) -> ExternalDecisionRequest:
    regime_rebalance = config.strategies.regime_rebalance
    policy = regime_rebalance.target_weight_policy
    ratio_gate = getattr(regime_rebalance, "ratio_gate", None)
    account_metrics: dict[str, float] = {}
    for metric in AccountMetric:
        try:
            account_metrics[metric.value] = account.value(metric)
        except AccountingError:
            continue

    allowed_total_weight = target_weight_policy_total_limit(
        effective_weights,
        policy,
    )
    request = build_external_decision_request(
        decision_type=TARGET_WEIGHT_DECISION_TYPE,
        generated_at=generated_at,
        dry_run=dry_run,
        input_data={
            "strategy": {
                "name": "regime_rebalance",
                "weight_base": regime_rebalance.weight_base.value,
                "margin_usage": regime_margin_usage,
                "lookback_days": int(regime_rebalance.lookback_days),
                "soft_band": float(regime_rebalance.soft_band),
                "hard_band": float(regime_rebalance.hard_band),
                "hard_band_rebalance_fraction": float(
                    regime_rebalance.hard_band_rebalance_fraction
                ),
                "cooldown_days": int(regime_rebalance.cooldown_days),
                "last_rebalance_at": (
                    last_rebalance.astimezone(UTC).isoformat()
                    if last_rebalance is not None
                    else None
                ),
                "choppiness_min": float(regime_rebalance.choppiness_min),
                "efficiency_max": float(regime_rebalance.efficiency_max),
                "flow_trade_min": float(regime_rebalance.flow_trade_min),
                "flow_trade_stop": float(regime_rebalance.flow_trade_stop),
                "flow_imbalance_tau": float(regime_rebalance.flow_imbalance_tau),
                "deficit_rail_start": float(regime_rebalance.deficit_rail_start),
                "deficit_rail_stop": float(regime_rebalance.deficit_rail_stop),
                "ratio_gate": (
                    RatioGateInput.model_validate(
                        ratio_gate, from_attributes=True
                    ).model_dump(mode="json")
                    if ratio_gate is not None
                    else None
                ),
            },
            "account": {
                "metrics": account_metrics,
                "rebalance_base_value": total_value,
                "excluded_option_value": excluded_value,
            },
            "portfolio": {
                "configured_total_weight": sum(
                    float(symbol_configs[symbol].weight) for symbol in symbols
                ),
                "post_volatility_total_weight": sum(effective_weights.values()),
            },
            "symbols": {
                symbol: build_target_weight_symbol_input(
                    config,
                    symbol,
                    symbol_config=symbol_configs[symbol],
                    volatility_detail=volatility_details.get(symbol),
                    effective_weight=effective_weights[symbol],
                    total_value=total_value,
                    current_position=current_positions[symbol],
                    current_value=current_values[symbol],
                    market_price=market_prices[symbol],
                )
                for symbol in symbols
            },
            "adjustment_constraints": {
                symbol: MultiplierConstraints.model_validate(
                    limits, from_attributes=True
                ).model_dump(mode="json")
                for symbol, limits in policy.symbols.items()
            },
            "total_weight_constraint": {
                "max_total_weight": policy.max_total_weight,
                "effective_max_total_weight": allowed_total_weight,
                "default_prevents_additional_leverage": (
                    policy.max_total_weight is None
                ),
            },
            "market_data": market_data.request_input(),
        },
    )
    TargetWeightDecisionRequest.model_validate(request.model_dump(mode="json"))
    return request
