from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from thetagang.config_models import ExternalDecisionProviderConfig

EXTERNAL_DECISION_SCHEMA_VERSION = 1


class ExternalDecisionError(RuntimeError):
    """Raised when an external provider cannot return a valid decision."""


class _ResponseTooLargeError(RuntimeError):
    pass


class ExternalDecisionEnvelope(BaseModel):
    """Protocol identity shared by requests and responses."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    request_id: str = Field(..., min_length=1)
    decision_type: str = Field(..., min_length=1)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema_version(cls, value: Any) -> int:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class ExternalDecisionRequestEnvelope(ExternalDecisionEnvelope):
    generated_at: datetime
    dry_run: bool

    @field_validator("generated_at")
    @classmethod
    def require_generated_at_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        return value


class ExternalDecisionRequest(ExternalDecisionRequestEnvelope):
    input: dict[str, Any]


class DecisionInput(BaseModel):
    """JSON contract fields shared by the published decision-specific requests."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ExternalDecisionMarketData(DecisionInput):
    """Aligned completed-session data shared by market-based decisions."""

    source: str = "ibkr"
    timeframe: str
    what_to_show: str = "TRADES"
    regular_trading_hours_only: bool = True
    sessions: list[date] = Field(
        ..., min_length=1, description="Unique sessions ordered oldest to newest."
    )
    closes: dict[str, list[Annotated[float, Field(gt=0, strict=True)]]] = Field(
        ..., min_length=1
    )
    primary_exchanges: dict[str, str] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_alignment(self) -> ExternalDecisionMarketData:
        if any(
            earlier >= later for earlier, later in zip(self.sessions, self.sessions[1:])
        ):
            raise ValueError("market data sessions must be strictly increasing")
        symbols = set(self.closes)
        if symbols != set(self.primary_exchanges):
            raise ValueError("market data symbols and exchanges must match")
        if any(len(values) != len(self.sessions) for values in self.closes.values()):
            raise ValueError("market data close arrays must align with sessions")
        return self

    def request_input(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ExternalDecisionProducer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)


class ExternalDecisionResponseEnvelope(ExternalDecisionEnvelope):
    # Decision-specific validators may require a completed market session, but
    # the generic transport also supports decisions that are not market-based.
    as_of_session: date | None = None
    expires_at: datetime | None = None
    producer: ExternalDecisionProducer

    @field_validator("expires_at")
    @classmethod
    def require_expires_at_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value


class ExternalDecisionResponse(ExternalDecisionResponseEnvelope):
    output: dict[str, Any]


def build_external_decision_request(
    *,
    decision_type: str,
    generated_at: datetime,
    dry_run: bool,
    input_data: dict[str, Any],
) -> ExternalDecisionRequest:
    return ExternalDecisionRequest(
        schema_version=EXTERNAL_DECISION_SCHEMA_VERSION,
        request_id=str(uuid4()),
        decision_type=decision_type,
        generated_at=generated_at,
        dry_run=dry_run,
        input=input_data,
    )


def external_decision_response_metadata(
    response: ExternalDecisionResponse,
    *,
    provider: str,
) -> dict[str, Any]:
    return {
        "request_id": response.request_id,
        "provider": provider,
        "producer": response.producer.name,
        "producer_version": response.producer.version,
        "as_of_session": (
            response.as_of_session.isoformat() if response.as_of_session else None
        ),
        "expires_at": (
            response.expires_at.isoformat() if response.expires_at else None
        ),
    }


class ExternalDecisionProvider(Protocol):
    async def decide(
        self, request: ExternalDecisionRequest
    ) -> ExternalDecisionResponse: ...


class CommandExternalDecisionProvider:
    """Invoke a provider command with JSON on stdin and stdout."""

    def __init__(self, config: ExternalDecisionProviderConfig) -> None:
        self._config = config

    @staticmethod
    async def _write_request(
        stream: asyncio.StreamWriter, request_bytes: bytes
    ) -> None:
        try:
            stream.write(request_bytes)
            await stream.drain()
        except (BrokenPipeError, ConnectionResetError):
            # Match communicate(): an early provider exit is reported through
            # its return code or invalid response instead of the stdin pipe.
            pass
        finally:
            stream.close()

    @staticmethod
    async def _read_response(
        stream: asyncio.StreamReader, max_response_bytes: int
    ) -> bytes:
        chunks: list[bytes] = []
        response_bytes = 0
        while chunk := await stream.read(65_536):
            response_bytes += len(chunk)
            if response_bytes > max_response_bytes:
                raise _ResponseTooLargeError
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    async def _read_stderr(stream: asyncio.StreamReader) -> bytes:
        captured = bytearray()
        while chunk := await stream.read(65_536):
            if len(captured) < 512:
                remaining = 512 - len(captured)
                captured.extend(chunk[:remaining])
        return bytes(captured)

    @staticmethod
    async def _discard_output(stream: asyncio.StreamReader) -> None:
        while await stream.read(65_536):
            pass

    @classmethod
    async def _kill_and_wait(cls, process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "posix":
                # Descendants can inherit the pipes even after the command exits.
                os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                process.kill()
        except ProcessLookupError:
            pass
        # wait() alone can deadlock when a pipe's reader paused its transport
        # after hitting the buffer limit. Drain without retaining more output.
        await asyncio.gather(
            *(
                cls._discard_output(stream)
                for stream in (process.stdout, process.stderr)
                if stream is not None
            ),
            process.wait(),
        )

    async def decide(
        self, request: ExternalDecisionRequest
    ) -> ExternalDecisionResponse:
        try:
            request_bytes = json.dumps(
                request.model_dump(mode="json"),
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ExternalDecisionError(
                "external decision request is not valid JSON"
            ) from exc

        cwd = None
        if self._config.working_directory is not None:
            cwd = str(Path(self._config.working_directory).expanduser())

        try:
            process = await asyncio.create_subprocess_exec(
                *self._config.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise ExternalDecisionError(
                "external decision provider could not be started"
            ) from exc

        if process.stdin is None or process.stdout is None or process.stderr is None:
            await self._kill_and_wait(process)
            raise ExternalDecisionError(
                "external decision provider pipes could not be created"
            )

        tasks = [
            asyncio.create_task(self._write_request(process.stdin, request_bytes)),
            asyncio.create_task(
                self._read_response(
                    process.stdout,
                    self._config.max_response_bytes,
                )
            ),
            asyncio.create_task(self._read_stderr(process.stderr)),
            asyncio.create_task(process.wait()),
        ]
        communication = asyncio.gather(*tasks)
        try:
            _, stdout, stderr, _ = await asyncio.wait_for(
                communication,
                timeout=self._config.timeout_seconds,
            )
        except TimeoutError as exc:
            raise ExternalDecisionError("external decision provider timed out") from exc
        except _ResponseTooLargeError as exc:
            raise ExternalDecisionError(
                "external decision response is too large"
            ) from exc
        finally:
            # Stop all readers before cleanup takes ownership of the pipes.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._kill_and_wait(process)

        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").strip()
            error_suffix = f": {error_text[:512]}" if error_text else ""
            raise ExternalDecisionError(
                f"external decision provider exited with status "
                f"{process.returncode}{error_suffix}"
            )
        return parse_external_decision_response(stdout)


def parse_external_decision_response(stdout: bytes) -> ExternalDecisionResponse:
    try:
        decoded = json.loads(
            stdout.decode("utf-8"),
            parse_constant=lambda value: _raise_invalid_constant(value),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExternalDecisionError(
            "external decision provider returned invalid JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise ExternalDecisionError(
            "external decision provider response must be a JSON object"
        )
    try:
        return ExternalDecisionResponse.model_validate(decoded)
    except ValueError as exc:
        raise ExternalDecisionError(
            "external decision provider returned an invalid response envelope"
        ) from exc


def _raise_invalid_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


class ExternalDecisionProviders:
    """Named provider registry shared by decision points across the application."""

    def __init__(
        self, providers: Mapping[str, ExternalDecisionProviderConfig] | None = None
    ) -> None:
        self._providers: dict[str, ExternalDecisionProvider] = {}
        configured_providers = providers if isinstance(providers, Mapping) else {}
        for name, config in configured_providers.items():
            if config.transport == "command":
                self._providers[name] = CommandExternalDecisionProvider(config)

    def replace(self, name: str, provider: ExternalDecisionProvider) -> None:
        """Replace a provider, primarily for embedding and deterministic tests."""

        self._providers[name] = provider

    async def decide(
        self, provider_name: str, request: ExternalDecisionRequest
    ) -> ExternalDecisionResponse:
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ExternalDecisionError(
                f"external decision provider is not configured: {provider_name}"
            )
        response = await provider.decide(request)
        validate_response_identity(response, request)
        return response


def validate_response_identity(
    response: ExternalDecisionResponseEnvelope, request: ExternalDecisionRequestEnvelope
) -> None:
    if response.request_id != request.request_id:
        raise ExternalDecisionError("external decision response request_id mismatch")
    if response.decision_type != request.decision_type:
        raise ExternalDecisionError("external decision response type mismatch")


def validate_market_decision_response(
    response: ExternalDecisionResponse,
    *,
    history_dates: list[date],
    max_signal_age_sessions: int,
    now: datetime,
    decision_name: str,
) -> date:
    """Validate freshness shared by completed-session market decisions."""

    if not history_dates:
        raise ExternalDecisionError(
            f"{decision_name} requires completed-session market data"
        )
    latest_session = history_dates[-1]
    signal_session = response.as_of_session
    if signal_session is None:
        raise ExternalDecisionError(f"{decision_name} response requires as_of_session")
    if signal_session > latest_session:
        raise ExternalDecisionError(f"{decision_name} signal is from the future")
    try:
        signal_index = history_dates.index(signal_session)
    except ValueError as exc:
        raise ExternalDecisionError(
            f"{decision_name} signal is not aligned to a supplied session"
        ) from exc
    signal_age_sessions = len(history_dates) - signal_index - 1
    if signal_age_sessions > max_signal_age_sessions:
        raise ExternalDecisionError(f"{decision_name} signal is stale")

    validate_decision_expiry(response.expires_at, now=now, decision_name=decision_name)
    return signal_session


def validate_decision_expiry(
    expires_at: datetime | None, *, now: datetime, decision_name: str
) -> None:
    if expires_at is not None:
        if expires_at.utcoffset() is None:
            raise ExternalDecisionError(
                f"{decision_name} expires_at must include a timezone"
            )
        if expires_at.astimezone(UTC) <= now.astimezone(UTC):
            raise ExternalDecisionError(f"{decision_name} signal has expired")
