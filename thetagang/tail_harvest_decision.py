from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from thetagang.config_models import TailHarvestDecisionConfig
from thetagang.external_decisions import (
    ExternalDecisionError,
    ExternalDecisionResponse,
    validate_market_decision_response,
)

TAIL_HARVEST_DECISION_TYPE = "tail_hedge_harvest"


class TailHarvestDecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    harvest: bool
    reason: str | None = None


def validate_tail_harvest_response(
    response: ExternalDecisionResponse,
    *,
    policy: TailHarvestDecisionConfig,
    history_dates: list[date],
    now: datetime,
) -> TailHarvestDecisionOutput:
    validate_market_decision_response(
        response,
        history_dates=history_dates,
        max_signal_age_sessions=policy.max_signal_age_sessions,
        now=now,
        decision_name="tail harvest decision",
    )
    if type(response.output.get("harvest")) is not bool:
        raise ExternalDecisionError(
            "tail harvest decision returned an invalid harvest value"
        )
    try:
        return TailHarvestDecisionOutput.model_validate(response.output)
    except ValueError as exc:
        raise ExternalDecisionError(
            "tail harvest decision returned an invalid output"
        ) from exc
