import sys
from datetime import UTC, datetime

import pytest

from thetagang.config_models import ExternalDecisionProviderConfig
from thetagang.external_decisions import (
    ExternalDecisionError,
    ExternalDecisionMarketData,
    ExternalDecisionProviders,
    ExternalDecisionRequest,
)


def _request() -> ExternalDecisionRequest:
    return ExternalDecisionRequest(
        request_id="request-1",
        decision_type="test_decision",
        generated_at=datetime(2026, 9, 3, tzinfo=UTC),
        dry_run=True,
        input={"value": 7},
    )


def test_external_request_requires_timezone() -> None:
    with pytest.raises(ValueError, match="generated_at must include a timezone"):
        ExternalDecisionRequest(
            request_id="request-1",
            decision_type="test_decision",
            generated_at=datetime(2026, 9, 3),  # noqa: DTZ001
            dry_run=True,
            input={},
        )


def test_external_market_data_serializes_aligned_sessions() -> None:
    market_data = ExternalDecisionMarketData(
        timeframe="1 day",
        sessions=[datetime(2026, 9, 2, tzinfo=UTC).date()],
        closes={"TQQQ": [42.0]},
        primary_exchanges={"TQQQ": "NASDAQ"},
    )

    assert market_data.request_input() == {
        "source": "ibkr",
        "timeframe": "1 day",
        "what_to_show": "TRADES",
        "regular_trading_hours_only": True,
        "sessions": ["2026-09-02"],
        "closes": {"TQQQ": [42.0]},
        "primary_exchanges": {"TQQQ": "NASDAQ"},
    }


@pytest.mark.parametrize(
    ("closes", "primary_exchanges", "error"),
    [
        ({"TQQQ": [42.0, 43.0]}, {"TQQQ": "NASDAQ"}, "align"),
        ({"TQQQ": [42.0]}, {"QQQ": "NASDAQ"}, "symbols"),
    ],
)
def test_external_market_data_rejects_inconsistent_symbols_or_lengths(
    closes: dict[str, list[float]],
    primary_exchanges: dict[str, str],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        ExternalDecisionMarketData(
            timeframe="1 day",
            sessions=[datetime(2026, 9, 2, tzinfo=UTC).date()],
            closes=closes,
            primary_exchanges=primary_exchanges,
        )


@pytest.mark.asyncio
async def test_command_provider_round_trips_versioned_json() -> None:
    script = """
import json
import sys

request = json.load(sys.stdin)
json.dump({
    "schema_version": 1,
    "request_id": request["request_id"],
    "decision_type": request["decision_type"],
    "producer": {"name": "fixture", "version": "1"},
    "output": {"answer": request["input"]["value"] * 2},
}, sys.stdout)
"""
    providers = ExternalDecisionProviders(
        {
            "fixture": ExternalDecisionProviderConfig(
                command=[sys.executable, "-c", script]
            )
        }
    )

    response = await providers.decide("fixture", _request())

    assert response.producer.name == "fixture"
    assert response.output == {"answer": 14}
    assert response.as_of_session is None


@pytest.mark.asyncio
async def test_command_provider_rejects_non_json_output() -> None:
    providers = ExternalDecisionProviders(
        {
            "fixture": ExternalDecisionProviderConfig(
                command=[sys.executable, "-c", "print('not json')"]
            )
        }
    )

    with pytest.raises(ExternalDecisionError, match="invalid JSON"):
        await providers.decide("fixture", _request())


@pytest.mark.asyncio
async def test_command_provider_enforces_timeout() -> None:
    providers = ExternalDecisionProviders(
        {
            "fixture": ExternalDecisionProviderConfig(
                command=[sys.executable, "-c", "import time; time.sleep(1)"],
                timeout_seconds=0.01,
            )
        }
    )

    with pytest.raises(ExternalDecisionError, match="timed out"):
        await providers.decide("fixture", _request())


@pytest.mark.asyncio
async def test_command_provider_stops_oversized_response() -> None:
    providers = ExternalDecisionProviders(
        {
            "fixture": ExternalDecisionProviderConfig(
                command=[sys.executable, "-c", "print('x' * 2048)"],
                max_response_bytes=1024,
            )
        }
    )

    with pytest.raises(ExternalDecisionError, match="too large"):
        await providers.decide("fixture", _request())


@pytest.mark.asyncio
async def test_provider_registry_checks_response_identity() -> None:
    script = """
import json
import sys

json.load(sys.stdin)
json.dump({
    "schema_version": 1,
    "request_id": "wrong-request",
    "decision_type": "test_decision",
    "as_of_session": "2026-09-02",
    "producer": {"name": "fixture", "version": "1"},
    "output": {},
}, sys.stdout)
"""
    providers = ExternalDecisionProviders(
        {
            "fixture": ExternalDecisionProviderConfig(
                command=[sys.executable, "-c", script]
            )
        }
    )

    with pytest.raises(ExternalDecisionError, match="request_id mismatch"):
        await providers.decide("fixture", _request())
