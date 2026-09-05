from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from thetagang.config_models import TailHarvestDecisionConfig, TailHedgeConfig
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

TAIL_HARVEST_DECISION_TYPE = "tail_hedge_harvest"


class TailHarvestStrategyInput(DecisionInput):
    name: Literal["tail_hedge"]
    annual_budget: float
    regime_weight_base: Literal["net_liq", "net_liq_ex_options", "managed_stocks"]
    regime_margin_usage: float


class TailHarvestHostConstraints(DecisionInput):
    baseline_band_triggered: Literal[True]
    requires_approved_same_symbol_hard_underweight_buy: Literal[True]
    state_owned_active_profitable_puts_only: Literal[True]
    host_selects_contracts_quantities_and_limit_prices: Literal[True]


class TailHarvestAccountInput(DecisionInput):
    net_liquidation: float
    regime_rebalance_base: float
    excluded_option_value: float


class PlannedHarvestSale(DecisionInput):
    entry_id: str
    symbol: str
    con_id: int
    expiration: str
    quantity: int
    limit_price: float
    estimated_gross_proceeds: float
    estimated_fees: float
    estimated_net_proceeds: float


class HarvestOpportunityInput(DecisionInput):
    sleeve_value: float
    sleeve_weight: float
    harvest_trigger_weight: float
    harvest_target_weight: float
    target_sleeve_value: float
    sale_budget: float
    approved_rebalance_value: float
    planned_sales: list[PlannedHarvestSale]


class UnderlyingBrokerPosition(DecisionInput):
    shares: float
    market_value: float
    average_cost_per_share: float | None
    unrealized_pnl: float
    realized_pnl: float


class TailProgramInput(DecisionInput):
    budget_weight: float
    entries_per_year: int
    entry_gate: Literal["vix", "none"]
    entry_vix_max: float
    target_dte: int
    min_dte: int
    max_dte: int
    exit_dte: int
    minimum_open_interest: int
    minimum_bid: float
    max_bid_ask_ratio: float
    max_premium_ratio: float
    catastrophe_drawdowns: list[float]


# Diagnostics describe preceding host calculations, not provider instructions.
# Keys can vary by calculation availability and status; consumers tolerate new keys.
ModifierDiagnostics = dict[str, str | bool | int | float | date | None]


class TargetModifierInputs(DecisionInput):
    volatility_weight: ModifierDiagnostics | None
    target_weight_policy: ModifierDiagnostics | None
    absolute_trend: ModifierDiagnostics | None


class ProtectedUnderlyingInput(DecisionInput):
    configured_weight: float
    primary_exchange: str
    market_price: float | None
    current_shares: float | None
    current_value: float | None
    broker_position: UnderlyingBrokerPosition
    current_weight: float | None
    target_weight: float | None
    target_value: float | None
    target_shares: float | None
    approved_buy_shares: int
    approved_buy_value: float | None
    tail_program: TailProgramInput
    target_modifiers: TargetModifierInputs


class HedgeContractInput(DecisionInput):
    con_id: int
    expiration: str
    strike: float
    right: Literal["P"]
    multiplier: float | None


class HarvestCandidateInput(DecisionInput):
    quantity: int
    gross_proceeds_per_contract: float
    estimated_fee_per_contract: float
    net_proceeds_per_contract: float
    cost_basis_per_contract: float
    profit_multiple: float


class HedgePositionInput(DecisionInput):
    entry_id: str
    symbol: str
    status: str
    contract: HedgeContractInput
    state_owned_quantity: int
    live_position_quantity: float | None
    reported_owned_market_value: float | None
    live_position_market_value: float | None
    live_average_cost_per_contract: float | None
    live_realized_pnl: float | None
    state_owned_unrealized_pnl: float | None
    quoted_limit_price: float | None
    entered_at: AwareDatetime
    entry_limit_price: float
    estimated_cost: float
    recovered_cost: float
    unrecovered_cost: float
    host_candidate: bool
    candidate: HarvestCandidateInput | None


class TailHarvestDecisionInput(DecisionInput):
    """USD values, fractional weights, and whole option contract quantities."""

    strategy: TailHarvestStrategyInput
    host_constraints: TailHarvestHostConstraints
    account: TailHarvestAccountInput
    opportunity: HarvestOpportunityInput
    underlyings: dict[str, ProtectedUnderlyingInput]
    hedge_positions: list[HedgePositionInput]
    market_data: ExternalDecisionMarketData


class TailHarvestDecisionRequest(ExternalDecisionRequestEnvelope):
    decision_type: Literal["tail_hedge_harvest"]
    input: TailHarvestDecisionInput


class TailHarvestDecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    harvest: bool = Field(strict=True)
    reason: str | None = None


class TailHarvestDecisionResponse(ExternalDecisionResponseEnvelope):
    decision_type: Literal["tail_hedge_harvest"]
    as_of_session: date
    output: TailHarvestDecisionOutput


def build_tail_harvest_request(
    *,
    generated_at: datetime,
    dry_run: bool,
    tail_hedge: TailHedgeConfig,
    regime_weight_base: str,
    regime_margin_usage: float,
    account: dict[str, float],
    opportunity: dict[str, Any],
    underlyings: dict[str, dict[str, Any]],
    hedge_positions: list[dict[str, Any]],
    market_data: ExternalDecisionMarketData,
) -> ExternalDecisionRequest:
    request = build_external_decision_request(
        decision_type=TAIL_HARVEST_DECISION_TYPE,
        generated_at=generated_at,
        dry_run=dry_run,
        input_data={
            "strategy": {
                "name": "tail_hedge",
                "annual_budget": float(tail_hedge.annual_budget),
                "regime_weight_base": regime_weight_base,
                "regime_margin_usage": regime_margin_usage,
            },
            "host_constraints": {
                "baseline_band_triggered": True,
                "requires_approved_same_symbol_hard_underweight_buy": True,
                "state_owned_active_profitable_puts_only": True,
                "host_selects_contracts_quantities_and_limit_prices": True,
            },
            "account": account,
            "opportunity": opportunity,
            "underlyings": underlyings,
            "hedge_positions": hedge_positions,
            "market_data": market_data.request_input(),
        },
    )
    TailHarvestDecisionRequest.model_validate(request.model_dump(mode="json"))
    return request


def validate_tail_harvest_response(
    response: ExternalDecisionResponse,
    *,
    policy: TailHarvestDecisionConfig,
    history_dates: list[date],
    now: datetime,
) -> TailHarvestDecisionOutput:
    validate_market_decision_response(
        response,
        history_dates=history_dates,
        max_signal_age_sessions=policy.max_signal_age_sessions,
        now=now,
        decision_name="tail harvest decision",
    )
    try:
        return TailHarvestDecisionOutput.model_validate(response.output)
    except ValueError as exc:
        raise ExternalDecisionError(
            "tail harvest decision returned an invalid output"
        ) from exc
