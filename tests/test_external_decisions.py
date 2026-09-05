import os
import subprocess
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
        schema_version=1,
        request_id="request-1",
        decision_type="test_decision",
        generated_at=datetime(2026, 9, 3, tzinfo=UTC),
        dry_run=True,
        input={"value": 7},
    )


def test_external_request_requires_timezone() -> None:
    with pytest.raises(ValueError, match="generated_at must include a timezone"):
        ExternalDecisionRequest(
            schema_version=1,
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


@pytest.mark.parametrize("failure", ["stdout", "stderr", "descendant", "cancel"])
def test_command_provider_cleanup_cannot_hang(failure: str) -> None:
    if failure == "descendant" and os.name != "posix":
        pytest.skip("Process-group cleanup requires POSIX")
    # Isolate the event loop so a pipe-draining regression fails within a hard
    # deadline, even if cancellation of decide() itself deadlocks.
    driver = """
import asyncio
import sys
import time
from datetime import UTC, datetime
from thetagang.config_models import ExternalDecisionProviderConfig
from thetagang.external_decisions import (
    CommandExternalDecisionProvider, ExternalDecisionError, ExternalDecisionRequest,
)

async def main():
    failure = sys.argv[1]
    scripts = {
        'stdout': 'import sys; sys.stdout.write("x" * 2_000_000); sys.stdout.flush()',
        'stderr': 'import sys, time; sys.stderr.write("x" * 2_000_000); time.sleep(2)',
        'descendant': 'import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])',
        'cancel': 'import time; time.sleep(2)',
    }
    provider = CommandExternalDecisionProvider(ExternalDecisionProviderConfig(
        command=[sys.executable, '-c', scripts[failure]],
        max_response_bytes=1024, timeout_seconds=0.3 if failure != 'stdout' else 1,
    ))
    request = ExternalDecisionRequest(schema_version=1, request_id='test', decision_type='test',
        generated_at=datetime.now(UTC), dry_run=True, input={})
    start = time.monotonic()
    task = asyncio.create_task(provider.decide(request))
    if failure == 'cancel':
        await asyncio.sleep(0.1)
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        assert failure == 'cancel'
    except ExternalDecisionError as exc:
        assert ('too large' if failure == 'stdout' else 'timed out') in str(exc)
    else:
        raise AssertionError('Provider unexpectedly succeeded')
    assert time.monotonic() - start < 1.5

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", driver, failure],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


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
