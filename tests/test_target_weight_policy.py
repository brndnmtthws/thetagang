from datetime import UTC, date, datetime, timedelta

import pytest

from thetagang.config_models import TargetWeightPolicyConfig
from thetagang.external_decisions import (
    ExternalDecisionError,
    ExternalDecisionResponse,
)
from thetagang.target_weight_policy import validate_target_weight_response


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
