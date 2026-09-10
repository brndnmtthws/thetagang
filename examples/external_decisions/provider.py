"""Standard-library protocol example; replace decide() with your model inference.

Run with your provider environment's Python. No ThetaGang imports are needed.
This is a neutral sizing / harvest-veto example, not a trading recommendation.

The host publishes the resolved absolute-trend policy (mode, exit_depth,
min_dwell_sessions) in every request. A provider whose sizing assumes a
particular rule must pass that rule as the only command argument; a host
deployment that does not match it exactly is rejected instead of silently
diverging from the model's assumptions.
"""

from __future__ import annotations

import json
import sys
from typing import Any

POLICY_FIELDS = ("mode", "exit_depth", "min_dwell_sessions")
POLICY_MODES = ("cliff", "deadband")


def expected_policy() -> dict[str, Any] | None:
    if len(sys.argv) < 2:
        return None
    policy = json.loads(sys.argv[1])
    if not isinstance(policy, dict) or set(policy) != set(POLICY_FIELDS):
        raise ValueError(f"expected absolute-trend policy must use {POLICY_FIELDS}")
    return policy


def published_policy(symbol: str, trend: dict[str, Any]) -> dict[str, Any]:
    """Return the resolved policy in whichever shape the request publishes it.

    Target-weight requests publish the nested `absolute_trend.policy` object.
    Harvest modifier diagnostics describe the same rule with flat keys.
    """

    nested = trend.get("policy")
    if nested is not None:
        if not isinstance(nested, dict) or set(nested) != set(POLICY_FIELDS):
            raise ValueError(f"{symbol}: absolute trend policy fields are missing")
        return nested
    policy = {field: trend.get(field) for field in POLICY_FIELDS}
    missing = sorted(field for field, value in policy.items() if value is None)
    if missing:
        raise ValueError(
            f"{symbol}: absolute trend policy is missing {', '.join(missing)}"
        )
    return policy


def validate_policy(symbol: str, policy: Any, expected: dict[str, Any] | None) -> None:
    """Fail loudly unless the host resolved exactly the rule the provider models."""

    if not isinstance(policy, dict) or set(policy) != set(POLICY_FIELDS):
        raise ValueError(f"{symbol}: absolute trend policy fields are missing")
    if not isinstance(policy["exit_depth"], (int, float)) or isinstance(
        policy["exit_depth"], bool
    ):
        raise TypeError(f"{symbol}: exit_depth must be a number")
    if not isinstance(policy["min_dwell_sessions"], int) or isinstance(
        policy["min_dwell_sessions"], bool
    ):
        raise TypeError(f"{symbol}: min_dwell_sessions must be an integer")
    if policy["mode"] not in POLICY_MODES:
        raise ValueError(f"{symbol}: unknown absolute trend mode: {policy['mode']}")
    # Mirror the host's validation exactly: a cliff carries no hysteresis, and a
    # deadband must carry enough of it to differ from a cliff.
    hysteresis = policy["exit_depth"] > 0 or policy["min_dwell_sessions"] >= 2
    if policy["mode"] == "cliff" and (
        policy["exit_depth"] > 0 or policy["min_dwell_sessions"] > 0
    ):
        raise ValueError(
            f"{symbol}: cliff absolute trend policy must not set hysteresis"
        )
    if policy["mode"] == "deadband" and not hysteresis:
        raise ValueError(
            f"{symbol}: deadband absolute trend policy requires hysteresis"
        )
    if expected is not None and policy != expected:
        raise ValueError(
            f"{symbol}: host absolute trend policy {policy} does not match the "
            f"provider's expected policy {expected}"
        )


def validate_request_policies(
    request: dict[str, Any], expected: dict[str, Any] | None
) -> None:
    context = request["input"]
    if request["decision_type"] == "regime_target_weights":
        candidates = {
            symbol: symbol_input.get("absolute_trend")
            for symbol, symbol_input in context["symbols"].items()
        }
        active = [
            (symbol, trend)
            for symbol, trend in candidates.items()
            if trend is not None and trend.get("enabled", False)
        ]
    else:
        # Harvest modifier diagnostics are only published for active modifiers.
        active = [
            (symbol, underlying["target_modifiers"]["absolute_trend"])
            for symbol, underlying in context["underlyings"].items()
            if underlying.get("target_modifiers", {}).get("absolute_trend") is not None
        ]
    for symbol, trend in active:
        validate_policy(symbol, published_policy(symbol, trend), expected)


def decide(request: dict[str, Any]) -> dict[str, Any]:
    context = request["input"]
    if request["decision_type"] == "regime_target_weights":
        return {
            "adjustments": {
                symbol: {"multiplier": 1.0, "reason": "reference-baseline"}
                for symbol in context["adjustment_constraints"]
            }
        }
    if request["decision_type"] == "tail_hedge_harvest":
        return {"harvest": False, "reason": "reference-veto"}
    raise ValueError("Unsupported decision_type")


def main() -> None:
    request = json.load(sys.stdin)
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise ValueError("Unsupported schema_version")
    try:
        validate_request_policies(request, expected_policy())
    except (TypeError, ValueError) as exc:
        print(f"absolute trend policy rejected: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    response = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "decision_type": request["decision_type"],
        "as_of_session": request["input"]["market_data"]["sessions"][-1],
        "producer": {"name": "reference-provider", "version": "1"},
        "output": decide(request),
    }
    json.dump(response, sys.stdout, allow_nan=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
