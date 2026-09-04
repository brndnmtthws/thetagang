import json
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from thetagang.decision_check import (
    CONTRACT_MODELS,
    cli,
    published_schemas,
    read_request,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "external_decisions"
DECISIONS = ("regime_target_weights", "tail_hedge_harvest")


@pytest.mark.parametrize("name", CONTRACT_MODELS)
def test_published_fixtures_and_schemas_match_contract(name: str) -> None:
    model = CONTRACT_MODELS[name]
    fixture = EXAMPLES / f"{name}.json"
    model.model_validate_json(fixture.read_bytes())
    filename = f"{name}.v1.schema.json"
    checked_in = ROOT / "docs" / "external-decisions" / "schemas" / filename
    assert json.loads(checked_in.read_text()) == published_schemas()[filename]


@pytest.mark.parametrize("decision", DECISIONS)
def test_reference_provider_runs_with_only_standard_library(decision: str) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "check",
            "--request",
            str(EXAMPLES / f"{decision}.request.json"),
            "--",
            sys.executable,
            "-I",
            "-S",
            str(EXAMPLES / "provider.py"),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["valid"] is True
    assert report["decision_type"] == decision
    expected = json.loads((EXAMPLES / f"{decision}.response.json").read_text())
    assert report["output"] == expected["output"]
    if decision == "regime_target_weights":
        assert report["post_policy_weights"] == {"TQQQ": 0.4, "QQQ": 0.5}


def replay(
    tmp_path: Path,
    *,
    decision: str = "regime_target_weights",
    request: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    options: tuple[str, ...] = (),
) -> Any:
    request_path = EXAMPLES / f"{decision}.request.json"
    if request is not None:
        request_path = tmp_path / "request.json"
        request_path.write_text(json.dumps(request))
    response_path = EXAMPLES / f"{decision}.response.json"
    if response is not None:
        response_path = tmp_path / "response.json"
        response_path.write_text(json.dumps(response))
    return CliRunner().invoke(
        cli,
        [
            "check",
            "--request",
            str(request_path),
            "--response",
            str(response_path),
            *options,
        ],
    )


@pytest.mark.parametrize("decision", DECISIONS)
def test_saved_response_replay_uses_request_time(tmp_path: Path, decision: str) -> None:
    result = replay(tmp_path, decision=decision)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["validated_at"] == "2026-09-04T14:30:00+00:00"


@pytest.mark.parametrize("decision", DECISIONS)
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("request_id", "another-request", "request_id mismatch"),
        ("decision_type", "another-hook", "type mismatch"),
        ("schema_version", 2, "invalid response envelope"),
        ("as_of_session", "2026-09-02", "signal is stale"),
        ("expires_at", "2026-09-04T14:29:00Z", "signal has expired"),
    ],
)
def test_replay_rejects_invalid_response(
    tmp_path: Path, decision: str, field: str, value: object, error: str
) -> None:
    response = json.loads((EXAMPLES / f"{decision}.response.json").read_text())
    response[field] = value
    result = replay(tmp_path, decision=decision, response=response)
    assert result.exit_code == 1
    assert error in result.output


def test_replay_can_select_signal_age_and_validation_time(tmp_path: Path) -> None:
    response = json.loads(
        (EXAMPLES / "regime_target_weights.response.json").read_text()
    )
    response["as_of_session"] = "2026-09-02"
    response["expires_at"] = "2026-09-05T00:00:00Z"
    accepted = replay(
        tmp_path, response=response, options=("--max-signal-age-sessions", "1")
    )
    assert accepted.exit_code == 0, accepted.output
    expired = replay(
        tmp_path,
        response=response,
        options=(
            "--max-signal-age-sessions",
            "1",
            "--at",
            "2026-09-05T00:00:00Z",
        ),
    )
    assert expired.exit_code == 1
    assert "signal has expired" in expired.output


@pytest.mark.parametrize("multiplier", [True, "1.0", 1.2, float("nan")])
def test_replay_rejects_invalid_multiplier(tmp_path: Path, multiplier: object) -> None:
    response = json.loads(
        (EXAMPLES / "regime_target_weights.response.json").read_text()
    )
    response["output"]["adjustments"]["TQQQ"]["multiplier"] = multiplier
    result = replay(tmp_path, response=response)
    assert result.exit_code == 1


def test_replay_uses_host_total_weight_limit(tmp_path: Path) -> None:
    request = json.loads((EXAMPLES / "regime_target_weights.request.json").read_text())
    limits = request["input"]["adjustment_constraints"]["TQQQ"]
    limits.update(max_multiplier=2.0, clamp_to_volatility_bounds=False)
    response = json.loads(
        (EXAMPLES / "regime_target_weights.response.json").read_text()
    )
    response["output"]["adjustments"]["TQQQ"]["multiplier"] = 2.0
    result = replay(tmp_path, request=request, response=response)
    assert result.exit_code == 1
    assert "exceeds the permitted total weight" in result.output

    request["input"]["total_weight_constraint"].update(
        max_total_weight=1.3,
        effective_max_total_weight=1.3,
        default_prevents_additional_leverage=False,
    )
    result = replay(tmp_path, request=request, response=response)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["post_policy_weights"] == {"TQQQ": 0.8, "QQQ": 0.5}


@pytest.mark.parametrize("decision", DECISIONS)
def test_invalid_request_is_rejected_before_provider_execution(
    tmp_path: Path, decision: str
) -> None:
    request = json.loads((EXAMPLES / f"{decision}.request.json").read_text())
    request["input"]["market_data"]["closes"]["TQQQ"].pop()
    path = tmp_path / "bad-request.json"
    path.write_text(json.dumps(request))
    result = CliRunner().invoke(
        cli, ["check", "--request", str(path), "--", "nonexistent-provider"]
    )
    assert result.exit_code == 1
    assert "align with sessions" in result.output
    assert "could not be started" not in result.output


def test_contract_rejects_missing_nested_context(tmp_path: Path) -> None:
    request = json.loads((EXAMPLES / "tail_hedge_harvest.request.json").read_text())
    del request["input"]["hedge_positions"][0]["contract"]["multiplier"]
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    with pytest.raises(ValueError, match="multiplier"):
        read_request(path)


def test_provider_failure_does_not_report_baseline_success() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "check",
            "--request",
            str(EXAMPLES / "regime_target_weights.request.json"),
            "--",
            sys.executable,
            "-c",
            "print('not JSON')",
        ],
    )
    assert result.exit_code == 1
    assert "invalid JSON" in result.output


@pytest.mark.parametrize("epsilon", ["nan", "inf", "-1", "0"])
def test_replay_rejects_invalid_weight_tolerance(tmp_path: Path, epsilon: str) -> None:
    result = replay(tmp_path, options=("--weight-epsilon", epsilon))
    assert result.exit_code == 2
    assert "finite and positive" in result.output


def test_schema_export(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["schemas", "--output-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(list(tmp_path.glob("*.schema.json"))) == 4
