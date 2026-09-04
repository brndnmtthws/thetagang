"""Offline provider conformance checks: python -m thetagang.decision_check."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from pydantic import AwareDatetime, BaseModel, TypeAdapter

from thetagang.config_models import (
    ExternalDecisionProviderConfig,
    TailHarvestDecisionConfig,
    TargetWeightPolicyConfig,
)
from thetagang.external_decisions import (
    ExternalDecisionError,
    ExternalDecisionProviders,
    ExternalDecisionRequest,
    ExternalDecisionResponse,
    parse_external_decision_response,
    validate_response_identity,
)
from thetagang.tail_harvest_decision import (
    TAIL_HARVEST_DECISION_TYPE,
    TailHarvestDecisionRequest,
    TailHarvestDecisionResponse,
    validate_tail_harvest_response,
)
from thetagang.target_weight_policy import (
    TARGET_WEIGHT_DECISION_TYPE,
    TargetWeightDecisionRequest,
    TargetWeightDecisionResponse,
    apply_target_weight_adjustments,
    validate_target_weight_response,
)

Request = TargetWeightDecisionRequest | TailHarvestDecisionRequest
CONTRACT_MODELS: dict[str, type[BaseModel]] = {
    "regime_target_weights.request": TargetWeightDecisionRequest,
    "regime_target_weights.response": TargetWeightDecisionResponse,
    "tail_hedge_harvest.request": TailHarvestDecisionRequest,
    "tail_hedge_harvest.response": TailHarvestDecisionResponse,
}


def published_schemas() -> dict[str, dict[str, Any]]:
    return {
        f"{name}.v1.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **model.model_json_schema(),
        }
        for name, model in CONTRACT_MODELS.items()
    }


def read_request(path: Path) -> Request:
    raw = path.read_bytes()
    envelope = ExternalDecisionRequest.model_validate_json(raw)
    if envelope.decision_type == TARGET_WEIGHT_DECISION_TYPE:
        return TargetWeightDecisionRequest.model_validate_json(raw)
    if envelope.decision_type == TAIL_HARVEST_DECISION_TYPE:
        return TailHarvestDecisionRequest.model_validate_json(raw)
    raise ExternalDecisionError(f"Unsupported decision type: {envelope.decision_type}")


def check_response(
    request: Request,
    response: ExternalDecisionResponse,
    *,
    at: datetime,
    max_signal_age_sessions: int,
    weight_epsilon: float,
) -> dict[str, Any]:
    """Use production validators and target allocation math against saved context."""

    validate_response_identity(response, request)
    result: dict[str, Any] = {
        "valid": True,
        "decision_type": request.decision_type,
        "validated_at": at.isoformat(),
        "producer": response.producer.model_dump(),
        "output": response.output,
    }
    if isinstance(request, TargetWeightDecisionRequest):
        context = request.input
        policy = TargetWeightPolicyConfig.model_validate(
            {
                "symbols": {
                    symbol: limits.model_dump()
                    for symbol, limits in context.adjustment_constraints.items()
                },
                "max_total_weight": context.total_weight_constraint.max_total_weight,
                "max_signal_age_sessions": max_signal_age_sessions,
            }
        )
        adjustments = validate_target_weight_response(
            response,
            policy=policy,
            history_dates=context.market_data.sessions,
            now=at,
        )
        bounds: dict[str, tuple[float, float]] = {}
        for symbol, limits in policy.symbols.items():
            if symbol not in context.symbols:
                raise ExternalDecisionError(f"Missing target symbol context: {symbol}")
            if limits.clamp_to_volatility_bounds:
                volatility = context.symbols[symbol].volatility_weight.config
                if volatility is None or not volatility.enabled:
                    raise ExternalDecisionError(f"Missing volatility bounds: {symbol}")
                bounds[symbol] = (volatility.min_weight, volatility.max_weight)
        weights, _ = apply_target_weight_adjustments(
            {
                symbol: item.post_volatility_weight
                for symbol, item in context.symbols.items()
            },
            adjustments,
            policy=policy,
            volatility_bounds=bounds,
            eps=weight_epsilon,
        )
        result["post_policy_weights"] = weights
    else:
        validate_tail_harvest_response(
            response,
            policy=TailHarvestDecisionConfig(
                max_signal_age_sessions=max_signal_age_sessions
            ),
            history_dates=request.input.market_data.sessions,
            now=at,
        )
    return result


@click.group()
def cli() -> None:
    """Check external decision providers without starting ThetaGang or IBKR."""


@cli.command()
@click.option(
    "--output-dir", required=True, type=click.Path(file_okay=False, path_type=Path)
)
def schemas(output_dir: Path) -> None:
    """Export versioned request and response JSON schemas."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, schema in published_schemas().items():
        (output_dir / filename).write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n"
        )
    click.echo(f"Wrote {len(CONTRACT_MODELS)} schemas to {output_dir}")


@cli.command(context_settings={"ignore_unknown_options": True})
@click.option(
    "--request",
    "request_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--response",
    "response_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--at",
    help="Replay validation time (ISO 8601 with offset); defaults to generated_at.",
)
@click.option(
    "--max-signal-age-sessions",
    default=0,
    type=click.IntRange(min=0),
    show_default=True,
)
@click.option("--weight-epsilon", default=1e-8, type=float, show_default=True)
@click.option("--timeout-seconds", default=10.0, type=float, show_default=True)
@click.option("--max-response-bytes", default=1_048_576, type=int, show_default=True)
@click.option(
    "--working-directory", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def check(
    request_path: Path,
    response_path: Path | None,
    at: str | None,
    max_signal_age_sessions: int,
    weight_epsilon: float,
    timeout_seconds: float,
    max_response_bytes: int,
    working_directory: Path | None,
    command: tuple[str, ...],
) -> None:
    """Replay REQUEST against COMMAND (after --), or validate a saved RESPONSE."""
    if bool(command) == (response_path is not None):
        raise click.UsageError("Supply either a command after -- or --response.")
    if not math.isfinite(weight_epsilon) or weight_epsilon <= 0:
        raise click.BadParameter(
            "must be finite and positive", param_hint="--weight-epsilon"
        )
    try:
        request = read_request(request_path)
        validation_time = (
            TypeAdapter(AwareDatetime).validate_python(at)
            if at
            else request.generated_at
        )
        transport = ExternalDecisionProviderConfig(
            command=list(command) or ["saved-response"],
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            working_directory=str(working_directory) if working_directory else None,
        )
        if response_path is None:
            providers = ExternalDecisionProviders({"offline": transport})
            response = asyncio.run(
                providers.decide(
                    "offline",
                    ExternalDecisionRequest.model_validate(
                        request.model_dump(mode="json")
                    ),
                )
            )
        else:
            with response_path.open("rb") as stream:
                raw = stream.read(transport.max_response_bytes + 1)
            if len(raw) > transport.max_response_bytes:
                raise ExternalDecisionError("external decision response is too large")
            response = parse_external_decision_response(raw)
        result = check_response(
            request,
            response,
            at=validation_time,
            max_signal_age_sessions=max_signal_age_sessions,
            weight_epsilon=weight_epsilon,
        )
    except (OSError, ValueError, ExternalDecisionError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    cli()
