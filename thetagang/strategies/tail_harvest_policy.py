from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class HarvestBandDecision:
    sleeve_value: float
    sleeve_weight: float
    trigger_value: float
    target_value: float
    sale_budget: float


def evaluate_harvest_band(
    *,
    net_liquidation: float,
    sleeve_value: float,
    trigger_weight: float,
    target_weight: float,
) -> HarvestBandDecision | None:
    """Return the value to harvest after a tail sleeve breaches its band."""
    values = (
        net_liquidation,
        sleeve_value,
        trigger_weight,
        target_weight,
    )
    if any(not math.isfinite(value) for value in values):
        return None
    if (
        net_liquidation <= 0.0
        or sleeve_value <= 0.0
        or trigger_weight <= 0.0
        or target_weight < 0.0
        or target_weight >= trigger_weight
    ):
        return None

    trigger_value = net_liquidation * trigger_weight
    if sleeve_value <= trigger_value:
        return None

    target_value = net_liquidation * target_weight
    sale_budget = sleeve_value - target_value
    if sale_budget <= 0.0:
        return None

    return HarvestBandDecision(
        sleeve_value=sleeve_value,
        sleeve_weight=sleeve_value / net_liquidation,
        trigger_value=trigger_value,
        target_value=target_value,
        sale_budget=sale_budget,
    )


def minimum_harvest_limit_price(
    *,
    quoted_limit_price: float,
    sleeve_value: float,
    trigger_value: float,
    profitable_limit_price: float,
) -> float:
    """Protect both profitability and the band condition during repricing."""
    values = (
        quoted_limit_price,
        sleeve_value,
        trigger_value,
        profitable_limit_price,
    )
    if any(not math.isfinite(value) or value <= 0.0 for value in values):
        raise ValueError("harvest limit inputs must be finite and positive")
    if trigger_value >= sleeve_value:
        raise ValueError("harvest sleeve must be above its trigger")

    trigger_ratio = trigger_value / sleeve_value
    band_threshold_cents = quoted_limit_price * trigger_ratio * 100.0
    band_limit_price = (math.floor(band_threshold_cents + 1e-9) + 1) / 100.0
    return max(profitable_limit_price, band_limit_price)
