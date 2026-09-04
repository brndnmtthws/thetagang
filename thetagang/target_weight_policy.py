from __future__ import annotations

import math
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from thetagang.config_models import TargetWeightPolicyConfig
from thetagang.external_decisions import (
    ExternalDecisionError,
    ExternalDecisionResponse,
    validate_market_decision_response,
)

TARGET_WEIGHT_DECISION_TYPE = "regime_target_weights"
TARGET_WEIGHT_POLICY_STATE_EVENT = "target_weight_policy_state"


class TargetWeightMultiplier(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    multiplier: float = Field(..., ge=0.0)
    reason: str | None = None


class TargetWeightDecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adjustments: dict[str, TargetWeightMultiplier]


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

    raw_adjustments = response.output.get("adjustments")
    if isinstance(raw_adjustments, dict):
        for raw_adjustment in raw_adjustments.values():
            if not isinstance(raw_adjustment, dict):
                continue
            multiplier = raw_adjustment.get("multiplier")
            if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
                raise ExternalDecisionError(
                    "target weight policy returned invalid adjustments"
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
        if not math.isfinite(multiplier):
            raise ExternalDecisionError(
                f"target weight policy multiplier for {symbol} is not finite"
            )
        if multiplier < limits.min_multiplier or multiplier > limits.max_multiplier:
            raise ExternalDecisionError(
                f"target weight policy multiplier for {symbol} is outside "
                "configured bounds"
            )
    return output.adjustments
