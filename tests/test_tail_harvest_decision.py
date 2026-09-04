from datetime import UTC, date, datetime, timedelta

import pytest

from thetagang.config_models import TailHarvestDecisionConfig
from thetagang.external_decisions import (
    ExternalDecisionError,
    ExternalDecisionResponse,
)
from thetagang.tail_harvest_decision import validate_tail_harvest_response


def _policy() -> TailHarvestDecisionConfig:
    return TailHarvestDecisionConfig(enabled=True, provider="fixture")


def _response(
    *,
    harvest: object = True,
    as_of_session: date | None = date(2026, 9, 2),
) -> ExternalDecisionResponse:
    return ExternalDecisionResponse(
        request_id="request-1",
        decision_type="tail_hedge_harvest",
        as_of_session=as_of_session,
        producer={"name": "fixture", "version": "1"},
        output={"harvest": harvest, "reason": "test"},
    )


@pytest.mark.parametrize("harvest", [True, False])
def test_tail_harvest_response_accepts_boolean_decision(harvest: bool) -> None:
    output = validate_tail_harvest_response(
        _response(harvest=harvest),
        policy=_policy(),
        history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
        now=datetime(2026, 9, 3, tzinfo=UTC),
    )

    assert output.harvest is harvest


@pytest.mark.parametrize("harvest", [1, 0, "true", None])
def test_tail_harvest_response_rejects_non_boolean_decision(
    harvest: object,
) -> None:
    with pytest.raises(ExternalDecisionError, match="invalid output"):
        validate_tail_harvest_response(
            _response(harvest=harvest),
            policy=_policy(),
            history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )


def test_tail_harvest_response_rejects_stale_session() -> None:
    with pytest.raises(ExternalDecisionError, match="stale"):
        validate_tail_harvest_response(
            _response(as_of_session=date(2026, 9, 1)),
            policy=_policy(),
            history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )


def test_tail_harvest_response_requires_market_session() -> None:
    with pytest.raises(ExternalDecisionError, match="requires as_of_session"):
        validate_tail_harvest_response(
            _response(as_of_session=None),
            policy=_policy(),
            history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )


def test_tail_harvest_response_rejects_expired_decision() -> None:
    response = _response()
    response.expires_at = datetime(2026, 9, 3, tzinfo=UTC) - timedelta(seconds=1)

    with pytest.raises(ExternalDecisionError, match="expired"):
        validate_tail_harvest_response(
            response,
            policy=_policy(),
            history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )


def test_tail_harvest_response_rejects_unknown_output_fields() -> None:
    response = _response()
    response.output["quantity"] = 2

    with pytest.raises(ExternalDecisionError, match="invalid output"):
        validate_tail_harvest_response(
            response,
            policy=_policy(),
            history_dates=[date(2026, 9, 1), date(2026, 9, 2)],
            now=datetime(2026, 9, 3, tzinfo=UTC),
        )
