from datetime import UTC, date, datetime, timedelta

import pytest

from thetagang.config_models import TargetWeightPolicyConfig
from thetagang.external_decisions import (
    ExternalDecisionError,
    ExternalDecisionResponse,
)
from thetagang.target_weight_policy import (
    TargetWeightMultiplier,
    apply_target_weight_adjustments,
    validate_target_weight_response,
)


def _policy() -> TargetWeightPolicyConfig:
    return TargetWeightPolicyConfig(
        enabled=True,
        provider="fixture",
        symbols={
            "TQQQ": {
                "min_multiplier": 0.8,
                "max_multiplier": 1.1,
                "clamp_to_volatility_bounds": True,
            }
        },
    )


def _response(
    *,
    as_of_session: date = date(2026, 9, 2),
    multiplier: object = 1.07,
    symbol: str = "TQQQ",
) -> ExternalDecisionResponse:
    return ExternalDecisionResponse(
        schema_version=1,
        request_id="request-1",
        decision_type="regime_target_weights",
        as_of_session=as_of_session,
        producer={"name": "fixture", "version": "1"},
        output={"adjustments": {symbol: {"multiplier": multiplier, "reason": "test"}}},
    )


@pytest.mark.parametrize("multiplier", [1, 1.07])
def test_target_weight_response_accepts_current_bounded_signal(
    multiplier: float,
) -> None:
    adjustments = validate_target_weight_response(
        _response(multiplier=multiplier),
        policy=_policy(),
        history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
        now=datetime(2026, 9, 3, tzinfo=UTC),
    )

    assert adjustments["TQQQ"].multiplier == pytest.approx(multiplier)


@pytest.mark.parametrize("multiplier", [0.79, 1.11, True, "1.0", float("nan")])
def test_target_weight_response_rejects_invalid_multiplier(
    multiplier: object,
) -> None:
    with pytest.raises(ExternalDecisionError, match="invalid|bounds"):
        validate_target_weight_response(
            _response(multiplier=multiplier),
            policy=_policy(),
            history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )


def test_target_weight_response_rejects_stale_session() -> None:
    with pytest.raises(ExternalDecisionError, match="stale"):
        validate_target_weight_response(
            _response(as_of_session=date(2026, 9, 1)),
            policy=_policy(),
            history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )


def test_target_weight_response_requires_market_session() -> None:
    response = _response().model_copy(update={"as_of_session": None})

    with pytest.raises(ExternalDecisionError, match="requires as_of_session"):
        validate_target_weight_response(
            response,
            policy=_policy(),
            history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )


def test_target_weight_response_rejects_expired_signal() -> None:
    response = _response()
    response.expires_at = datetime(2026, 9, 3, tzinfo=UTC) - timedelta(seconds=1)

    with pytest.raises(ExternalDecisionError, match="expired"):
        validate_target_weight_response(
            response,
            policy=_policy(),
            history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )


def test_target_weight_response_requires_configured_symbol_set() -> None:
    with pytest.raises(ExternalDecisionError, match="symbols do not match"):
        validate_target_weight_response(
            _response(symbol="QQQ"),
            policy=_policy(),
            history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("symbol", "baseline", "multiplier", "minimum", "maximum", "clamp", "expected"),
    [
        ("IBIT", 0.1875, 0.50, 0.10, 0.36, False, 0.10),
        ("IBIT", 0.30, 1.20, 0.10, 0.36, False, 0.36),
        ("TQQQ", 0.25, 0.50, 0.15, 0.55, False, 0.15),
        ("TQQQ", 0.50, 1.20, 0.15, 0.55, False, 0.55),
        ("IBIT", 0.1875, 0.50, 0.10, None, False, 0.10),
        ("IBIT", 0.30, 1.20, None, 0.36, False, 0.36),
        ("IBIT", 0.1875, 0.50, None, None, False, 0.09375),
        ("IBIT", 0.1875, 0.50, None, None, True, 0.1875),
        ("IBIT", 0.1875, 0.50, 0.10, 0.36, True, 0.1875),
        ("IBIT", 0.30, 1.20, 0.10, 0.36, True, 0.30),
        ("IBIT", 0.1875, 0.50, 0.20, 0.28, True, 0.20),
        ("IBIT", 0.30, 1.20, 0.20, 0.28, True, 0.28),
        ("IBIT", 0.30, 1.20, 0.0, 0.0, False, 0.0),
        ("TQQQ", 0.95, 1.20, None, 0.55, False, 0.55),
    ],
)
def test_target_bounds_apply_after_multiplication(
    symbol: str,
    baseline: float,
    multiplier: float,
    minimum: float | None,
    maximum: float | None,
    clamp: bool,
    expected: float,
) -> None:
    policy = TargetWeightPolicyConfig(
        symbols={
            symbol: {
                "min_multiplier": 0.50,
                "max_multiplier": 1.20,
                "min_target_weight": minimum,
                "max_target_weight": maximum,
                "clamp_to_volatility_bounds": clamp,
            }
        }
    )
    original_weights = {symbol: baseline}
    weights, details = apply_target_weight_adjustments(
        original_weights,
        {symbol: TargetWeightMultiplier(multiplier=multiplier)},
        policy=policy,
        volatility_bounds={symbol: (0.1875, 0.30)} if clamp else {},
        eps=1e-8,
    )
    assert weights[symbol] == pytest.approx(expected)
    assert details[symbol]["raw_weight"] == pytest.approx(baseline * multiplier)
    assert original_weights == {symbol: baseline}


@pytest.mark.parametrize(
    "bounds", [{"min_target_weight": 0.4}, {"max_target_weight": 0.1}]
)
def test_target_application_rejects_disjoint_clamps(bounds: dict[str, float]) -> None:
    policy = TargetWeightPolicyConfig(symbols={"IBIT": bounds})
    with pytest.raises(ExternalDecisionError, match="do not overlap volatility bounds"):
        apply_target_weight_adjustments(
            {"IBIT": 0.25},
            {"IBIT": TargetWeightMultiplier(multiplier=1.0)},
            policy=policy,
            volatility_bounds={"IBIT": (0.1875, 0.30)},
            eps=1e-8,
        )


def test_target_floors_cannot_bypass_total_exposure_limit() -> None:
    policy = TargetWeightPolicyConfig(
        symbols={
            symbol: {"min_target_weight": 0.6, "clamp_to_volatility_bounds": False}
            for symbol in ("TQQQ", "IBIT")
        }
    )
    with pytest.raises(ExternalDecisionError, match="permitted total weight"):
        apply_target_weight_adjustments(
            {"TQQQ": 0.4, "IBIT": 0.4},
            {
                symbol: TargetWeightMultiplier(multiplier=1.0)
                for symbol in policy.symbols
            },
            policy=policy,
            volatility_bounds={},
            eps=1e-8,
        )
