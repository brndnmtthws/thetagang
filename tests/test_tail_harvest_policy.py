import math

import pytest

from thetagang.strategies.tail_harvest_policy import (
    evaluate_harvest_band,
    minimum_harvest_limit_price,
)


def test_band_breach_sizes_sale_toward_target() -> None:
    decision = evaluate_harvest_band(
        portfolio_base_value=100_000.0,
        sleeve_value=7_000.0,
        trigger_weight=0.05,
        target_weight=0.03,
    )

    assert decision is not None
    assert decision.sleeve_weight == pytest.approx(0.07)
    assert decision.trigger_value == 5_000.0
    assert decision.target_value == 3_000.0
    assert decision.sale_budget == 4_000.0


def test_reprice_floor_preserves_a_barely_crossed_band() -> None:
    assert minimum_harvest_limit_price(
        quoted_limit_price=1.20,
        sleeve_value=120.0,
        trigger_value=100.0,
        profitable_limit_price=0.51,
    ) == pytest.approx(1.01)


def test_reprice_floor_is_strictly_above_an_exact_cent_trigger() -> None:
    assert minimum_harvest_limit_price(
        quoted_limit_price=2.02,
        sleeve_value=202.0,
        trigger_value=101.0,
        profitable_limit_price=0.51,
    ) == pytest.approx(1.02)


def test_reprice_floor_never_crosses_profitability() -> None:
    assert minimum_harvest_limit_price(
        quoted_limit_price=1.20,
        sleeve_value=400.0,
        trigger_value=100.0,
        profitable_limit_price=0.51,
    ) == pytest.approx(0.51)


@pytest.mark.parametrize("sleeve_value", [0.0, 5_000.0, 4_999.99])
def test_band_does_not_trigger_at_or_below_upper_bound(sleeve_value: float) -> None:
    assert (
        evaluate_harvest_band(
            portfolio_base_value=100_000.0,
            sleeve_value=sleeve_value,
            trigger_weight=0.05,
            target_weight=0.03,
        )
        is None
    )


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -1.0, 0.0])
def test_band_rejects_invalid_portfolio_base(bad_value: float) -> None:
    assert (
        evaluate_harvest_band(
            portfolio_base_value=bad_value,
            sleeve_value=7_000.0,
            trigger_weight=0.05,
            target_weight=0.03,
        )
        is None
    )
