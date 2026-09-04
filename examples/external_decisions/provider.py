"""Standard-library protocol example; replace decide() with your model inference.

Run with your provider environment's Python. No ThetaGang imports are needed.
This is a neutral sizing / harvest-veto example, not a trading recommendation.
"""

from __future__ import annotations

import json
import sys
from typing import Any


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
