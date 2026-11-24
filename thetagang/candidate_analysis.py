"""Candidate screening and portfolio-weight recommendation.

The module deliberately separates market-data collection from the allocation
logic.  A connected IBKR adapter (or another agent/data provider) can produce
``CandidateMetrics``; the deterministic analyzer then applies the same rules
regardless of where the data came from.  This makes a recommendation
reproducible and suitable for dry-run review before it is copied into the
main configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import re
import statistics
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

import tomlkit
from ib_async import IB, Option, Ticker

from thetagang.ibkr import IBKR, TickerField
from thetagang.options import option_dte
from thetagang.util import midpoint_or_market_price


class CandidateAnalysisError(ValueError):
    """Raised when an analysis cannot produce a valid allocation."""

    def __init__(
        self,
        message: str,
        *,
        assessments: Sequence[CandidateAssessment] = (),
        observations: Sequence[CandidateMetrics] = (),
    ) -> None:
        super().__init__(message)
        self.assessments = tuple(assessments)
        self.observations = tuple(observations)


MARKET_DATA_TYPE_NAMES = {
    1: "live",
    2: "frozen",
    3: "delayed",
    4: "delayed-frozen",
}
OPTION_FIELD_TIMEOUT_SECONDS = 10.0


def _finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_number(value: Any) -> Optional[float]:
    number = _finite_number(value)
    return number if number is not None and number >= 0 else None


class CandidateDataProvider(Protocol):
    """Provider contract for live IBKR or agent-backed observations."""

    async def fetch(self, symbol: str) -> "CandidateMetrics":
        """Fetch a point-in-time observation for ``symbol``."""


class EarningsDataProvider(Protocol):
    """Optional event-calendar provider used by live market data adapters."""

    async def days_until(self, symbol: str) -> Optional[int]:
        """Return days until the next earnings event, or ``None`` if unknown."""


async def collect_candidates(
    provider: CandidateDataProvider, symbols: Iterable[str]
) -> list["CandidateMetrics"]:
    """Collect candidates in the caller's order without changing the policy."""

    return [await provider.fetch(symbol.upper().strip()) for symbol in symbols]


class IBKRCandidateDataProvider:
    """Read-only candidate observations backed by a connected ``IBKR`` wrapper.

    The adapter intentionally samples the nearest-to-ATM Put and Call at one
    expiry near ``target_dte``.  This keeps the request bounded and mirrors the
    contracts that ThetaGang's option scanner can actually consider.  It does
    not submit, modify or cancel orders.
    """

    def __init__(
        self,
        ibkr: IBKR,
        *,
        target_dte: int = 45,
        primary_exchanges: Optional[Mapping[str, str]] = None,
        risk_groups: Optional[Mapping[str, str]] = None,
        earnings_provider: Optional[EarningsDataProvider] = None,
        historical_duration: str = "120 D",
        option_field_timeout: float = OPTION_FIELD_TIMEOUT_SECONDS,
        requested_market_data_type: Optional[int] = None,
    ) -> None:
        if not math.isfinite(option_field_timeout) or option_field_timeout <= 0:
            raise CandidateAnalysisError("option_field_timeout must be positive")
        self.ibkr = ibkr
        self.target_dte = target_dte
        self.primary_exchanges = {
            symbol.upper(): exchange
            for symbol, exchange in (primary_exchanges or {}).items()
        }
        self.risk_groups = {
            symbol.upper(): group for symbol, group in (risk_groups or {}).items()
        }
        self.earnings_provider = earnings_provider
        self.historical_duration = historical_duration
        self.option_field_timeout = option_field_timeout
        self.requested_market_data_type = requested_market_data_type

    async def fetch(self, symbol: str) -> CandidateMetrics:
        symbol = symbol.upper().strip()
        stock_ticker = await self.ibkr.get_ticker_for_stock(
            symbol, self.primary_exchanges.get(symbol, "")
        )
        if not stock_ticker.contract:
            raise CandidateAnalysisError(f"{symbol}: IBKR returned no stock contract")
        price = midpoint_or_market_price(stock_ticker)
        if not math.isfinite(price) or price <= 0:
            raise CandidateAnalysisError(f"{symbol}: no usable underlying price")

        chains = await self.ibkr.get_chains_for_contract(stock_ticker.contract)
        chain = self._select_chain(chains, symbol)
        expiration = self._select_expiration(chain.expirations)
        strikes = sorted(chain.strikes, key=lambda strike: abs(strike - price))
        if not strikes:
            raise CandidateAnalysisError(f"{symbol}: option chain has no strikes")
        strike = strikes[0]
        exchange = getattr(stock_ticker.contract, "exchange", "SMART") or "SMART"
        contracts = [
            Option(symbol, expiration, strike, right, exchange) for right in ("P", "C")
        ]
        qualified = await self.ibkr.qualify_contracts(*contracts)
        if len(qualified) != len(contracts):
            raise CandidateAnalysisError(
                f"{symbol}: unable to qualify target-dte option contracts"
            )
        tickers = await self.ibkr.get_tickers_for_contracts(
            symbol,
            qualified,
            generic_tick_list="100,101",
            required_fields=[],
            optional_fields=[
                TickerField.MARKET_PRICE,
                TickerField.GREEKS,
                TickerField.OPEN_INTEREST,
                TickerField.VOLUME,
                TickerField.MIDPOINT,
            ],
            field_timeout=self.option_field_timeout,
        )
        dte = option_dte(expiration)
        metrics = self._option_metrics(tickers, price, dte)
        bars = await self.ibkr.request_historical_data(
            stock_ticker.contract, self.historical_duration
        )
        realized_volatility = self._realized_volatility(bars)
        earnings_days = (
            await self.earnings_provider.days_until(symbol)
            if self.earnings_provider is not None
            else None
        )
        notes = list(metrics.pop("notes", ()))
        risk_group = self.risk_groups.get(symbol)
        if risk_group is None:
            risk_group = f"unclassified:{symbol}"
            notes.append("risk group was not supplied; correlation cap is unverified")
        return CandidateMetrics(
            symbol=symbol,
            price=price,
            dte=dte,
            realized_volatility=realized_volatility,
            earnings_days=earnings_days,
            source="ibkr",
            as_of=datetime.now(timezone.utc).isoformat(),
            requested_market_data_type=self.requested_market_data_type,
            risk_group=risk_group,
            notes=tuple(notes),
            **metrics,
        )

    def _select_chain(self, chains: Sequence[Any], symbol: str) -> Any:
        matching = [
            chain for chain in chains if getattr(chain, "tradingClass", "") == symbol
        ]
        if matching:
            return matching[0]
        if chains:
            return chains[0]
        raise CandidateAnalysisError(f"{symbol}: IBKR returned no option chain")

    def _select_expiration(self, expirations: Iterable[str]) -> str:
        candidates = [
            (abs(option_dte(expiration) - self.target_dte), expiration)
            for expiration in expirations
            if option_dte(expiration) > 0
        ]
        if not candidates:
            raise CandidateAnalysisError(
                "Option chain has no future expiration suitable for analysis"
            )
        return min(candidates)[1]

    @staticmethod
    def _option_metrics(
        tickers: Sequence[Ticker], price: float, dte: int
    ) -> dict[str, Any]:
        spreads: list[float] = []
        volumes: list[float] = []
        open_interests: list[float] = []
        premiums: list[float] = []
        implied_volatilities: list[float] = []
        notes: list[str] = []
        samples: list[dict[str, Any]] = []
        for ticker in tickers:
            contract = ticker.contract
            bid = _nonnegative_number(ticker.bid)
            ask = _nonnegative_number(ticker.ask)
            if bid is not None and ask is not None and bid > 0 and ask >= bid:
                midpoint = (bid + ask) / 2
                spreads.append((ask - bid) / midpoint)
                premiums.append(midpoint)

            # tick 8/74 is this contract's volume. Do not replace a genuine zero
            # with generic putVolume/callVolume from a different data scope.
            volume = _nonnegative_number(ticker.volume)
            if volume is not None:
                volumes.append(volume)
            oi_field = (
                "putOpenInterest"
                if contract and contract.right == "P"
                else "callOpenInterest"
            )
            open_interest = _nonnegative_number(getattr(ticker, oi_field))
            if open_interest is not None:
                open_interests.append(open_interest)

            # Contract IV comes from option computations (ticks 10-13/80-83).
            # ticker.impliedVolatility is the distinct 30-day underlying IV.
            implied_volatility = None
            iv_source = None
            delta = None
            greek_values: dict[str, dict[str, Optional[float]]] = {}
            for source in ("modelGreeks", "bidGreeks", "askGreeks", "lastGreeks"):
                computation = getattr(ticker, source)
                iv = _nonnegative_number(getattr(computation, "impliedVol", None))
                raw_delta = _finite_number(getattr(computation, "delta", None))
                valid_delta = (
                    raw_delta
                    if raw_delta is not None and -1 <= raw_delta <= 1
                    else None
                )
                greek_values[source] = {"impliedVol": iv, "delta": valid_delta}
                if implied_volatility is None and iv is not None and iv > 0:
                    implied_volatility, iv_source = iv, source
                if delta is None and valid_delta is not None:
                    delta = valid_delta
            if implied_volatility is not None:
                implied_volatilities.append(implied_volatility)
            label = (
                contract.localSymbol
                or f"{contract.lastTradeDateOrContractMonth} "
                f"{contract.right} {contract.strike:g}"
                if contract
                else "unknown contract"
            )
            samples.append(
                {
                    "contract": label,
                    "con_id": contract.conId if contract else None,
                    "right": contract.right if contract else None,
                    "bid": bid,
                    "ask": ask,
                    "volume": volume,
                    "open_interest": open_interest,
                    "open_interest_field": oi_field,
                    "implied_volatility": implied_volatility,
                    "iv_source": iv_source,
                    "delta": delta,
                    "greeks": greek_values,
                    "market_data_type": ticker.marketDataType,
                    # This is the last received ticker update, not an exchange quote timestamp.
                    "received_at": ticker.time.isoformat() if ticker.time else None,
                }
            )
        if len(implied_volatilities) < len(tickers):
            notes.append(
                "contract implied volatility is unavailable for some sampled options"
            )
        if any(sample["delta"] is None for sample in samples):
            notes.append("option delta is unavailable for some sampled options")

        def complete_min(values: Sequence[float]) -> Optional[float]:
            return (
                min(values)
                if len(values) == len(tickers) and len(tickers) == 2
                else None
            )

        return {
            "bid_ask_pct": max(spreads) if len(spreads) == len(tickers) == 2 else None,
            "option_volume": complete_min(volumes),
            "open_interest": complete_min(open_interests),
            "option_yield": (
                statistics.fmean(premiums) / price * 365 / max(dte, 1)
                if premiums
                else None
            ),
            "implied_volatility": (
                statistics.fmean(implied_volatilities) if implied_volatilities else None
            ),
            "notes": tuple(dict.fromkeys(notes)),
            "option_samples": tuple(samples),
        }

    @staticmethod
    def _realized_volatility(bars: Iterable[Any]) -> Optional[float]:
        closes: list[float] = []
        for bar in bars:
            try:
                close = float(getattr(bar, "close"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(close) and close > 0:
                closes.append(close)
        if len(closes) < 3:
            return None
        returns = [
            math.log(current / previous)
            for previous, current in zip(closes, closes[1:])
            if previous > 0 and current > 0
        ]
        if len(returns) < 2:
            return None
        return statistics.stdev(returns) * math.sqrt(252)


@dataclass(frozen=True)
class CandidateMetrics:
    """Market and risk observations for one candidate underlying.

    The first release requires the core option-chain fields.  The remaining
    fields are optional so a provider can be introduced incrementally.
    Percentages are represented as decimals (``0.01`` means 1%).
    """

    symbol: str
    price: float
    bid_ask_pct: Optional[float]
    option_volume: Optional[float]
    open_interest: Optional[float]
    dte: int
    earnings_days: Optional[int] = None
    implied_volatility: Optional[float] = None
    realized_volatility: Optional[float] = None
    beta: Optional[float] = None
    option_yield: Optional[float] = None
    stress_loss_pct: Optional[float] = None
    risk_group: str = "unclassified"
    source: str = "manual"
    as_of: Optional[str] = None
    notes: tuple[str, ...] = ()
    requested_market_data_type: Optional[int] = None
    option_samples: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CandidateMetrics":
        required = (
            "symbol",
            "price",
            "bid_ask_pct",
            "option_volume",
            "open_interest",
            "dte",
        )
        missing = [key for key in required if key not in data]
        if missing:
            raise CandidateAnalysisError(
                f"Candidate is missing required fields: {', '.join(missing)}"
            )
        raw_notes = data.get("notes", ())
        if raw_notes is None:
            notes = ()
        elif isinstance(raw_notes, str):
            notes = (raw_notes,)
        else:
            notes = tuple(str(note) for note in raw_notes)
        values = dict(data)
        values["notes"] = notes
        values["symbol"] = str(values["symbol"]).upper().strip()
        if not values["symbol"]:
            raise CandidateAnalysisError("Candidate symbol must not be empty")
        values["risk_group"] = str(values.get("risk_group", "unclassified"))
        try:
            values["price"] = float(values["price"])
            for key in ("bid_ask_pct", "option_volume", "open_interest"):
                if values[key] is not None:
                    values[key] = float(values[key])
            values["dte"] = int(values["dte"])
            for key in (
                "earnings_days",
                "implied_volatility",
                "realized_volatility",
                "beta",
                "option_yield",
                "stress_loss_pct",
            ):
                if values.get(key) is not None:
                    values[key] = (
                        int(values[key])
                        if key == "earnings_days"
                        else float(values[key])
                    )
        except (TypeError, ValueError) as exc:
            raise CandidateAnalysisError(
                f"Candidate {values['symbol']} contains a non-numeric metric"
            ) from exc
        field_names = {item.name for item in fields(cls)}
        return cls(**{key: values[key] for key in field_names if key in values})


@dataclass(frozen=True)
class AnalysisPolicy:
    """Hard filters and portfolio constraints used by the analyzer."""

    target_dte: int = 45
    dte_tolerance: int = 15
    max_bid_ask_pct: float = 0.05
    min_option_volume: float = 100.0
    min_open_interest: float = 500.0
    earnings_blackout_days: int = 14
    max_single_weight: float = 0.30
    max_group_weight: float = 0.50
    min_weight: float = 0.0
    capital: Optional[float] = None
    margin_usage: float = 0.50
    contract_multiplier: int = 100
    max_contract_notional_pct: float = 0.25
    stress_shock_pct: float = 0.25
    max_stress_loss_pct: float = 0.20
    allocation_step: float = 0.01
    risk_floor: float = 0.05

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AnalysisPolicy":
        values = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
        return cls(**values)

    def validate(self) -> None:
        if self.target_dte <= 0 or self.dte_tolerance < 0:
            raise CandidateAnalysisError("target_dte must be positive")
        if not 0 < self.max_single_weight <= 1:
            raise CandidateAnalysisError("max_single_weight must be in (0, 1]")
        if not 0 < self.max_group_weight <= 1:
            raise CandidateAnalysisError("max_group_weight must be in (0, 1]")
        if not 0 < self.allocation_step <= 0.10:
            raise CandidateAnalysisError("allocation_step must be in (0, 0.10]")
        if self.capital is not None and self.capital <= 0:
            raise CandidateAnalysisError("capital must be positive when provided")
        if not 0 < self.margin_usage <= 1:
            raise CandidateAnalysisError("margin_usage must be in (0, 1]")


@dataclass(frozen=True)
class CandidateAssessment:
    symbol: str
    eligible: bool
    score: float
    liquidity_score: float
    dte_score: float
    yield_score: float
    risk_score: float
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.missing_data:
            return "data_insufficient"
        return "eligible" if self.eligible else "rejected"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "status": self.status}


@dataclass(frozen=True)
class WeightRecommendation:
    symbol: str
    weight: float
    target_value: Optional[float]
    estimated_contract_notional_pct: Optional[float]
    score: float
    risk_group: str
    rationale: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateAnalysisReport:
    policy: AnalysisPolicy
    assessments: tuple[CandidateAssessment, ...]
    recommendations: tuple[WeightRecommendation, ...]
    rejected_symbols: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    observations: tuple[CandidateMetrics, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": asdict(self.policy),
            "assessments": [item.to_dict() for item in self.assessments],
            "observations": [asdict(item) for item in self.observations],
            "recommendations": [asdict(item) for item in self.recommendations],
            "rejected_symbols": list(self.rejected_symbols),
            "warnings": list(self.warnings),
        }


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _score_liquidity(metrics: CandidateMetrics, policy: AnalysisPolicy) -> float:
    spread_value = _nonnegative_number(metrics.bid_ask_pct)
    spread = (
        _clamp(1.0 - spread_value / policy.max_bid_ask_pct)
        if spread_value is not None
        else 0.0
    )
    volume = _clamp(
        math.log1p(_nonnegative_number(metrics.option_volume) or 0.0)
        / math.log1p(max(policy.min_option_volume * 10, 1.0))
    )
    open_interest = _clamp(
        math.log1p(_nonnegative_number(metrics.open_interest) or 0.0)
        / math.log1p(max(policy.min_open_interest * 10, 1.0))
    )
    return 0.50 * spread + 0.25 * volume + 0.25 * open_interest


def _score_dte(metrics: CandidateMetrics, policy: AnalysisPolicy) -> float:
    distance = abs(metrics.dte - policy.target_dte)
    return _clamp(1.0 - distance / max(policy.target_dte, 1))


def _score_yield(metrics: CandidateMetrics) -> float:
    if metrics.option_yield is None:
        return 0.5
    # 5% annualized option yield is treated as a strong, but not unlimited,
    # signal.  Yield is intentionally only one component of the score.
    return _clamp(metrics.option_yield / 0.05)


def _estimated_stress_loss(metrics: CandidateMetrics, policy: AnalysisPolicy) -> float:
    if metrics.stress_loss_pct is not None:
        return max(metrics.stress_loss_pct, policy.risk_floor)
    volatility = metrics.realized_volatility or metrics.implied_volatility
    if volatility is None:
        return max(policy.stress_shock_pct, policy.risk_floor)
    # A simple one-shock proxy.  A provider may supply a more complete
    # scenario loss directly through stress_loss_pct.
    return max(policy.stress_shock_pct * max(1.0, volatility / 0.20), policy.risk_floor)


def _score_risk(metrics: CandidateMetrics, policy: AnalysisPolicy) -> float:
    loss = _estimated_stress_loss(metrics, policy)
    budget = max(policy.max_stress_loss_pct, policy.risk_floor)
    return _clamp(1.0 / (1.0 + loss / budget))


def assess_candidate(
    metrics: CandidateMetrics, policy: AnalysisPolicy
) -> CandidateAssessment:
    reasons: list[str] = []
    missing_data: list[str] = []
    warnings = [
        note
        for note in metrics.notes
        if note != "earnings date was not supplied by the data provider"
    ]
    if not math.isfinite(metrics.price) or metrics.price <= 0:
        reasons.append("price must be positive")
    spread = _nonnegative_number(metrics.bid_ask_pct)
    volume = _nonnegative_number(metrics.option_volume)
    open_interest = _nonnegative_number(metrics.open_interest)
    if spread is None:
        missing_data.append("bid_ask_pct")
    elif spread > policy.max_bid_ask_pct:
        reasons.append(f"bid-ask spread exceeds {policy.max_bid_ask_pct:.1%}")
    if volume is None:
        missing_data.append("option_volume")
    elif volume < policy.min_option_volume:
        reasons.append(f"option volume is below {policy.min_option_volume:g}")
    if open_interest is None:
        missing_data.append("open_interest")
    elif open_interest < policy.min_open_interest:
        reasons.append(f"open interest is below {policy.min_open_interest:g}")
    if abs(metrics.dte - policy.target_dte) > policy.dte_tolerance:
        reasons.append(
            f"DTE {metrics.dte} is outside target range "
            f"[{policy.target_dte - policy.dte_tolerance}, "
            f"{policy.target_dte + policy.dte_tolerance}]"
        )
    if (
        metrics.earnings_days is not None
        and metrics.earnings_days >= 0
        and metrics.earnings_days < policy.earnings_blackout_days
    ):
        reasons.append(
            f"earnings event is {metrics.earnings_days} days away "
            f"(< {policy.earnings_blackout_days})"
        )
    if metrics.earnings_days is None:
        warnings.append("earnings date was not supplied; event risk is unverified")
    if (
        metrics.implied_volatility is not None
        and metrics.realized_volatility is not None
    ):
        if metrics.implied_volatility < metrics.realized_volatility:
            warnings.append("implied volatility is below realized volatility")
    if metrics.beta is not None and abs(metrics.beta) > 1.5:
        warnings.append(f"high beta exposure ({metrics.beta:.2f})")

    liquidity_score = _score_liquidity(metrics, policy)
    dte_score = _score_dte(metrics, policy)
    yield_score = _score_yield(metrics)
    risk_score = _score_risk(metrics, policy)
    score = (
        0.35 * liquidity_score
        + 0.15 * dte_score
        + 0.15 * yield_score
        + 0.35 * risk_score
    )
    return CandidateAssessment(
        symbol=metrics.symbol,
        eligible=not reasons and not missing_data,
        score=round(_clamp(score), 6),
        liquidity_score=round(liquidity_score, 6),
        dte_score=round(dte_score, 6),
        yield_score=round(yield_score, 6),
        risk_score=round(risk_score, 6),
        reasons=tuple(reasons),
        warnings=tuple(dict.fromkeys(warnings)),
        missing_data=tuple(missing_data),
    )


def _allocate_weights(
    metrics_by_symbol: Mapping[str, CandidateMetrics],
    assessments: Mapping[str, CandidateAssessment],
    policy: AnalysisPolicy,
) -> dict[str, float]:
    eligible = [item for item in assessments.values() if item.eligible]
    if not eligible:
        raise CandidateAnalysisError("No candidate passed the hard eligibility filters")
    if len(eligible) * policy.max_single_weight < 1.0 - 1e-9:
        raise CandidateAnalysisError(
            "Eligible candidates cannot reach 100% within max_single_weight"
        )
    group_capacity: dict[str, float] = {}
    for item in eligible:
        group = metrics_by_symbol[item.symbol].risk_group
        group_capacity.setdefault(group, policy.max_group_weight)
    if sum(group_capacity.values()) < 1.0 - 1e-9:
        raise CandidateAnalysisError(
            "Eligible risk groups cannot reach 100% within max_group_weight"
        )

    weights = {item.symbol: 0.0 for item in eligible}
    step = policy.allocation_step
    total = 0.0
    while total < 1.0 - 1e-9:
        ranked = sorted(
            eligible,
            key=lambda item: (
                item.score
                / (
                    1.0
                    + weights[item.symbol]
                    * _estimated_stress_loss(metrics_by_symbol[item.symbol], policy)
                ),
                item.score,
                item.symbol,
            ),
            reverse=True,
        )
        chosen: Optional[CandidateAssessment] = None
        for item in ranked:
            symbol = item.symbol
            group = metrics_by_symbol[symbol].risk_group
            if weights[symbol] + step <= policy.max_single_weight + 1e-9 and (
                sum(
                    weight
                    for other_symbol, weight in weights.items()
                    if metrics_by_symbol[other_symbol].risk_group == group
                )
                + step
                <= group_capacity[group] + 1e-9
            ):
                chosen = item
                break
        if chosen is None:
            # A smaller final increment avoids floating point and step-size
            # artifacts while preserving the hard caps.
            remainder = 1.0 - total
            for item in ranked:
                symbol = item.symbol
                group = metrics_by_symbol[symbol].risk_group
                group_weight = sum(
                    weight
                    for other_symbol, weight in weights.items()
                    if metrics_by_symbol[other_symbol].risk_group == group
                )
                if (
                    remainder <= policy.max_single_weight - weights[symbol] + 1e-9
                    and remainder <= group_capacity[group] - group_weight + 1e-9
                ):
                    chosen = item
                    step = remainder
                    break
        if chosen is None:
            raise CandidateAnalysisError(
                "Allocation constraints leave an unallocated remainder"
            )
        weights[chosen.symbol] += step
        total += step
    return {symbol: round(weight, 10) for symbol, weight in weights.items()}


def analyze_candidates(
    candidates: Iterable[CandidateMetrics],
    policy: Optional[AnalysisPolicy] = None,
) -> CandidateAnalysisReport:
    """Assess candidates and return a constrained, risk-adjusted allocation."""

    resolved_policy = policy or AnalysisPolicy()
    resolved_policy.validate()
    metrics = list(candidates)
    if not metrics:
        raise CandidateAnalysisError("At least one candidate is required")
    symbols = [item.symbol for item in metrics]
    if len(set(symbols)) != len(symbols):
        raise CandidateAnalysisError("Candidate symbols must be unique")
    metrics_by_symbol = {item.symbol: item for item in metrics}
    assessments_list = [assess_candidate(item, resolved_policy) for item in metrics]
    assessments = {item.symbol: item for item in assessments_list}
    try:
        weights = _allocate_weights(metrics_by_symbol, assessments, resolved_policy)
    except CandidateAnalysisError as exc:
        if not any(item.eligible for item in assessments_list):
            details = " | ".join(
                f"{item.symbol}: {' || '.join(item.reasons) or 'required data unavailable'}"
                for item in assessments_list
            )
            raise CandidateAnalysisError(
                f"{exc}. Rejections: {details}",
                assessments=assessments_list,
                observations=metrics,
            ) from exc
        raise CandidateAnalysisError(
            str(exc),
            assessments=assessments_list,
            observations=metrics,
        ) from exc

    recommendations: list[WeightRecommendation] = []
    report_warnings: list[str] = []
    for symbol, weight in sorted(weights.items(), key=lambda item: (-item[1], item[0])):
        item = metrics_by_symbol[symbol]
        target_value = (
            weight * resolved_policy.capital * resolved_policy.margin_usage
            if resolved_policy.capital is not None
            else None
        )
        contract_ratio = None
        rationale = ["passed hard filters", "risk-adjusted score allocation"]
        capital = resolved_policy.capital
        if target_value is not None and capital is not None:
            contract_notional = item.price * resolved_policy.contract_multiplier
            contract_ratio = contract_notional / (
                capital * resolved_policy.margin_usage
            )
            if contract_ratio > resolved_policy.max_contract_notional_pct:
                warning = (
                    f"{symbol}: one contract is {contract_ratio:.1%} of the "
                    "available capital base"
                )
                report_warnings.append(warning)
                rationale.append("review contract size before enabling writes")
        recommendations.append(
            WeightRecommendation(
                symbol=symbol,
                weight=weight,
                target_value=target_value,
                estimated_contract_notional_pct=contract_ratio,
                score=assessments[symbol].score,
                risk_group=item.risk_group,
                rationale=tuple(rationale),
            )
        )
    for assessment in assessments_list:
        report_warnings.extend(assessment.warnings)
    return CandidateAnalysisReport(
        policy=resolved_policy,
        assessments=tuple(assessments_list),
        recommendations=tuple(recommendations),
        rejected_symbols=tuple(
            item.symbol for item in assessments_list if not item.eligible
        ),
        warnings=tuple(dict.fromkeys(report_warnings)),
        observations=tuple(metrics),
    )


def _translate_reason(reason: str, language: str) -> str:
    if language == "en":
        return reason
    exact = {
        "price must be positive": "标的价格必须为正",
        "option ticker did not include liquidity data": "期权行情缺少流动性数据",
    }
    if reason in exact:
        return exact[reason]
    if reason.startswith("bid-ask spread exceeds "):
        return f"买卖价差超过 {reason.removeprefix('bid-ask spread exceeds ')}"
    if reason.startswith("option volume is below "):
        return f"期权成交量低于 {reason.removeprefix('option volume is below ')}"
    if reason.startswith("open interest is below "):
        return f"未平仓量低于 {reason.removeprefix('open interest is below ')}"
    dte_match = re.fullmatch(r"DTE (\d+) is outside target range \[(.+)\]", reason)
    if dte_match:
        return f"DTE 为 {dte_match.group(1)}，不在目标区间 [{dte_match.group(2)}] 内"
    earnings_match = re.fullmatch(
        r"earnings event is (\d+) days away \(< (\d+)\)", reason
    )
    if earnings_match:
        return (
            f"财报事件距离 {earnings_match.group(1)} 天，少于 "
            f"{earnings_match.group(2)} 天"
        )
    return reason


def _translate_warning(warning: str, language: str) -> str:
    if language == "en":
        return warning
    exact = {
        "earnings date was not supplied by the data provider": "数据提供方未提供财报日期",
        "earnings date was not supplied; event risk is unverified": "未提供财报日期；事件风险未验证",
        "implied volatility is below realized volatility": "隐含波动率低于已实现波动率",
        "one sampled option lacked a two-sided quote": "抽样期权缺少双边报价",
        "insufficient two-sided quotes for both Put and Call": "Put 和 Call 均缺少足够的双边报价",
        "option implied volatility and Greeks were unavailable": "期权隐含波动率和 Greeks 不可用",
        "contract implied volatility is unavailable for some sampled options": "部分抽样合约的隐含波动率未收到有效值",
        "option delta is unavailable for some sampled options": "部分抽样合约的 Delta 未收到有效值",
    }
    if warning in exact:
        return exact[warning]
    if warning.startswith("high beta exposure ("):
        return f"Beta 敞口较高（{warning.removeprefix('high beta exposure (').rstrip(')')}）"
    if warning.startswith("option bid/ask may be unavailable"):
        return "美国期权非正常交易时段可能没有 bid/ask；可尝试行情类型 2 或 4"
    if warning.startswith("risk group was not supplied"):
        return "未提供风险组；相关性上限未验证"
    contract_match = re.fullmatch(
        r"(.+): one contract is ([0-9.]+%) of the available capital base",
        warning,
    )
    if contract_match:
        return (
            f"{contract_match.group(1)}：一张合约占可用资金基数的 "
            f"{contract_match.group(2)}"
        )
    if warning == "Synthetic example values; replace with live observations.":
        return "示例数据为合成数据，请替换为实时观察值。"
    return warning


def _translate_cli_error(message: str, language: str) -> str:
    if language == "en":
        return message
    exact = {
        "No candidate passed the hard eligibility filters": "没有候选标的通过硬性筛选",
        "At least one candidate is required": "至少需要一个候选标的",
        "Eligible candidates cannot reach 100% within max_single_weight": "合格候选标的无法在单标的权重上限内分配到 100%",
        "Eligible risk groups cannot reach 100% within max_group_weight": "合格风险组无法在风险组权重上限内分配到 100%",
        "Allocation constraints leave an unallocated remainder": "分配约束导致仍有权重未分配",
    }
    if message in exact:
        return exact[message]
    prefix = "No candidate passed the hard eligibility filters. Rejections: "
    if message.startswith(prefix):
        details = []
        for item in message.removeprefix(prefix).split(" | "):
            symbol, _, raw_reasons = item.partition(": ")
            reasons = []
            for value in raw_reasons.split(" || "):
                translated = _translate_reason(value, language)
                if translated == value:
                    translated = _translate_warning(value, language)
                reasons.append(translated)
            details.append(f"{symbol}：{'，'.join(reasons)}")
        return "没有候选标的通过硬性筛选。拒绝原因：" + "；".join(details)
    return message


def render_diagnostics(
    assessments: Sequence[CandidateAssessment],
    observations: Sequence[CandidateMetrics],
    language: str = "zh",
) -> str:
    """Show evidence separately from threshold failures and optional warnings."""
    zh = language == "zh"
    statuses = {
        "eligible": "通过",
        "rejected": "不合格",
        "data_insufficient": "数据不足",
    }
    missing_names = {
        "bid_ask_pct": "双边报价",
        "option_volume": "期权成交量",
        "open_interest": "未平仓量",
    }
    by_symbol = {item.symbol: item for item in observations}

    def number(value: Any) -> str:
        finite = _finite_number(value)
        return f"{finite:g}" if finite is not None else ("缺失" if zh else "missing")

    lines: list[str] = []
    for assessment in assessments:
        status = statuses[assessment.status] if zh else assessment.status
        lines.append(f"{assessment.symbol}: {status}")
        if assessment.missing_data:
            names = [
                missing_names[key] if zh else key for key in assessment.missing_data
            ]
            label = (
                "缺失数据（至少一张抽样合约）"
                if zh
                else "Missing data (at least one sample)"
            )
            lines.append(f"  {label}: {', '.join(names)}")
        if assessment.reasons:
            reasons = [
                _translate_reason(reason, language) for reason in assessment.reasons
            ]
            label = "硬性拒绝" if zh else "Hard rejection"
            lines.append(f"  {label}: {'; '.join(reasons)}")
        if assessment.warnings:
            warnings = [
                _translate_warning(warning, language) for warning in assessment.warnings
            ]
            label = "警告（不单独导致拒绝）" if zh else "Warnings (non-blocking)"
            lines.append(f"  {label}: {'; '.join(warnings)}")
        observation = by_symbol.get(assessment.symbol)
        if observation is None:
            continue
        label = "汇总" if zh else "Aggregate"
        lines.append(
            f"  {label}: volume={number(observation.option_volume)}, "
            f"OI={number(observation.open_interest)}"
        )
        if observation.requested_market_data_type is not None:
            label = "请求行情类型" if zh else "Requested market data type"
            lines.append(f"  {label}: {observation.requested_market_data_type}")
        for sample in observation.option_samples:
            lines.append(
                f"  {sample['contract']} | bid={number(sample.get('bid'))} "
                f"ask={number(sample.get('ask'))} volume={number(sample.get('volume'))} "
                f"OI={number(sample.get('open_interest'))} "
                f"IV={number(sample.get('implied_volatility'))} "
                f"delta={number(sample.get('delta'))} "
                f"marketDataType={sample.get('market_data_type', '?')}"
            )
    return "\n".join(lines)


def _format_analysis_error(error: CandidateAnalysisError, language: str) -> str:
    if not error.assessments:
        return _translate_cli_error(str(error), language)
    message = str(error).split(". Rejections: ", 1)[0]
    return (
        _translate_cli_error(message, language)
        + "\n"
        + render_diagnostics(error.assessments, error.observations, language)
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    def json_safe(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        return value

    path.write_text(
        json.dumps(json_safe(payload), indent=2, ensure_ascii=False, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def render_markdown(report: CandidateAnalysisReport, language: str = "zh") -> str:
    """Render an auditable human-readable report in Chinese or English."""

    if language not in {"zh", "en"}:
        raise CandidateAnalysisError("language must be zh or en")
    if language == "zh":
        title = "# 候选标的分析与权重建议"
        introduction = (
            "本报告基于确定性规则生成。交易前请复核实时行情、期权链和财报日期。"
        )
        weights_title = "## 推荐权重"
        weights_header = "| 标的 | 权重 | 综合评分 | 风险组 | 目标金额 |"
        decisions_title = "## 候选评估"
        decisions_header = (
            "| 标的 | 是否合格 | 流动性评分 | DTE匹配 | 收益评分 | 风险评分 | 原因 |"
        )
        warnings_title = "## 风险提示"
        config_title = "## 配置片段"
        eligible_yes, eligible_no, empty_reason = "是", "否", "-"
    else:
        title = "# Candidate allocation review"
        introduction = (
            "This report is a deterministic screening result. Verify live quotes, "
            "option chain data and earnings dates before trading."
        )
        weights_title = "## Recommended weights"
        weights_header = "| Symbol | Weight | Score | Risk group | Target value |"
        decisions_title = "## Candidate decisions"
        decisions_header = (
            "| Symbol | Eligible | Liquidity | DTE | Yield | Risk | Reason |"
        )
        warnings_title = "## Warnings"
        config_title = "## Config snippet"
        eligible_yes, eligible_no, empty_reason = "yes", "no", "-"
    lines = [
        title,
        "",
        introduction,
        "",
        weights_title,
        "",
        weights_header,
        "| --- | ---: | ---: | --- | ---: |",
    ]
    for item in report.recommendations:
        target = f"{item.target_value:,.2f}" if item.target_value is not None else "-"
        lines.append(
            f"| {item.symbol} | {item.weight:.1%} | {item.score:.3f} | "
            f"{item.risk_group} | {target} |"
        )
    lines.extend(
        [
            "",
            decisions_title,
            "",
            decisions_header,
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in report.assessments:
        reason = (
            ("；" if language == "zh" else "; ").join(
                _translate_reason(value, language) for value in item.reasons
            )
            if item.reasons
            else empty_reason
        )
        status = eligible_yes if item.eligible else eligible_no
        if item.missing_data:
            status = "数据不足" if language == "zh" else "data_insufficient"
            reason = (
                ("缺失字段：" if language == "zh" else "Missing fields: ")
                + ", ".join(item.missing_data)
                + (f"; {reason}" if item.reasons else "")
            )
        lines.append(
            f"| {item.symbol} | {status} | "
            f"{item.liquidity_score:.3f} | {item.dte_score:.3f} | "
            f"{item.yield_score:.3f} | {item.risk_score:.3f} | {reason} |"
        )
    if report.warnings:
        lines.extend(["", warnings_title, ""])
        lines.extend(
            f"- {_translate_warning(warning, language)}" for warning in report.warnings
        )
    lines.extend(["", config_title, "", "```toml"])
    lines.append(render_toml(report).rstrip())
    lines.append("```")
    if report.observations:
        lines.extend(
            [
                "",
                "## 数据诊断" if language == "zh" else "## Data diagnostics",
                "",
                "```text",
                render_diagnostics(report.assessments, report.observations, language),
                "```",
            ]
        )
    return "\n".join(lines) + "\n"


def render_toml(report: CandidateAnalysisReport) -> str:
    """Render only the portfolio-symbol fragment for deliberate copy-in."""

    lines: list[str] = []
    for item in report.recommendations:
        symbol = item.symbol.replace('"', '\\"')
        header = (
            f"[symbols.{symbol}]"
            if all(char.isalnum() or char in "_-" for char in symbol)
            else f'[symbols."{symbol}"]'
        )
        lines.extend([header, f"weight = {item.weight:.4f}", ""])
    return "\n".join(lines)


def load_input(path: Path) -> tuple[list[CandidateMetrics], AnalysisPolicy]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise CandidateAnalysisError("Input JSON must be an object")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, Sequence) or isinstance(
        raw_candidates, (str, bytes)
    ):
        raise CandidateAnalysisError("Input JSON must contain a candidates array")
    if not all(isinstance(item, Mapping) for item in raw_candidates):
        raise CandidateAnalysisError("Every candidate must be a JSON object")
    candidates = [CandidateMetrics.from_mapping(item) for item in raw_candidates]
    policy = AnalysisPolicy.from_mapping(payload.get("policy", {}))
    return candidates, policy


def _resolve_market_data_type(
    configured_value: Any, override: Optional[int] = None
) -> int:
    """Resolve and validate the IBKR market-data type used by the analyzer."""

    value = override if override is not None else configured_value
    try:
        market_data_type = int(value)
    except (TypeError, ValueError) as exc:
        raise CandidateAnalysisError(
            "runtime.account.market_data_type must be an integer from 1 to 4"
        ) from exc
    if market_data_type not in MARKET_DATA_TYPE_NAMES:
        raise CandidateAnalysisError(
            "runtime.account.market_data_type must be an integer from 1 to 4"
        )
    return market_data_type


def _competing_session_message(market_data_type: int) -> str:
    message = (
        "IBKR error 10197: another live or paper session for this IBKR user is "
        "already consuming live market data. Close the competing session or use "
        "a separate IBKR username with its own market-data entitlement."
    )
    if market_data_type in (1, 2):
        message += (
            " For report-only analysis, delayed data can be tried with "
            "--market-data-type 3 where IBKR provides it."
        )
    else:
        message += (
            " IBKR rejected the request even though delayed data was selected; "
            "end the competing session before retrying."
        )
    return message


async def _collect_from_ibkr(
    config_path: Path,
    symbols: Sequence[str],
    policy: AnalysisPolicy,
    risk_groups: Optional[Mapping[str, str]] = None,
    market_data_type_override: Optional[int] = None,
    option_field_timeout: float = OPTION_FIELD_TIMEOUT_SECONDS,
) -> list[CandidateMetrics]:
    """Collect candidate observations through a read-only IBKR connection."""

    raw_config = tomlkit.parse(config_path.read_text(encoding="utf-8")).unwrap()
    runtime = raw_config.get("runtime", {})
    watchdog = runtime.get("watchdog", {})
    account = runtime.get("account", {})
    orders = runtime.get("orders", {})
    portfolio = raw_config.get("portfolio", {}).get("symbols", {})
    ib = IB()
    await ib.connectAsync(
        host=watchdog.get("host", "127.0.0.1"),
        port=int(watchdog.get("port", 7497)),
        clientId=int(watchdog.get("clientId", 1)),
        timeout=float(runtime.get("ib_async", {}).get("api_response_wait_time", 60)),
        readonly=True,
        account=str(account.get("number", "")),
    )
    ibkr = IBKR(
        ib,
        api_response_wait_time=int(
            runtime.get("ib_async", {}).get("api_response_wait_time", 60)
        ),
        default_order_exchange=str(orders.get("exchange", "SMART")),
        dry_run=True,
    )
    market_data_type = _resolve_market_data_type(
        account.get("market_data_type", 1), market_data_type_override
    )
    ibkr.set_market_data_type(market_data_type)
    primary_exchanges = {
        symbol.upper(): str(portfolio.get(symbol, {}).get("primary_exchange", ""))
        for symbol in symbols
    }
    provider = IBKRCandidateDataProvider(
        ibkr,
        target_dte=policy.target_dte,
        primary_exchanges=primary_exchanges,
        risk_groups=risk_groups,
        option_field_timeout=option_field_timeout,
        requested_market_data_type=market_data_type,
    )
    loop = asyncio.get_running_loop()
    competing_session = loop.create_future()
    data_errors: dict[str, list[str]] = {}

    def capture_market_data_error(
        request_id: int,
        error_code: int,
        error_string: str,
        contract: Any,
    ) -> None:
        if error_code == 10197 and not competing_session.done():
            competing_session.set_result(None)
        if error_code in {200, 354, 10089, 10090, 10167, 10168, 10186, 10197}:
            symbol = getattr(contract, "symbol", None) or "*"
            data_errors.setdefault(symbol, []).append(
                f"IBKR {error_code} (reqId={request_id}): {error_string}"
            )

    ib.errorEvent += capture_market_data_error
    try:
        collection = asyncio.create_task(collect_candidates(provider, symbols))
        done, _pending = await asyncio.wait(
            (collection, competing_session),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if competing_session in done:
            collection.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await collection
            raise CandidateAnalysisError(_competing_session_message(market_data_type))
        competing_session.cancel()
        observations = await collection
        return [
            replace(
                item,
                notes=tuple(
                    dict.fromkeys(
                        (
                            *item.notes,
                            *data_errors.get(item.symbol, ()),
                            *data_errors.get("*", ()),
                        )
                    )
                ),
            )
            for item in observations
        ]
    finally:
        ib.errorEvent -= capture_market_data_error
        ib.disconnect()


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Screen candidate underlyings and recommend ThetaGang weights."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Candidate JSON file")
    source.add_argument(
        "--ibkr",
        action="store_true",
        help="Collect observations from a read-only IBKR session",
    )
    parser.add_argument(
        "--config", type=Path, help="ThetaGang TOML (required with --ibkr)"
    )
    parser.add_argument(
        "--symbols", help="Comma-separated candidate symbols (required with --ibkr)"
    )
    parser.add_argument("--capital", type=float, help="Override analysis capital")
    parser.add_argument("--target-dte", type=int, default=45)
    parser.add_argument(
        "--option-field-timeout",
        type=float,
        default=OPTION_FIELD_TIMEOUT_SECONDS,
        help="Maximum seconds to wait for option fields (default: 10)",
    )
    parser.add_argument(
        "--language",
        choices=("zh", "en"),
        default="zh",
        help="Human-readable report language (default: zh)",
    )
    parser.add_argument(
        "--market-data-type",
        type=int,
        choices=tuple(MARKET_DATA_TYPE_NAMES),
        help=(
            "Override IBKR market data: 1=live, 2=frozen, 3=delayed, 4=delayed-frozen"
        ),
    )
    parser.add_argument(
        "--risk-groups",
        help="Optional comma-separated SYMBOL=GROUP mappings for --ibkr",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Markdown output path (default: alongside input)",
    )
    parser.add_argument(
        "--json-output", type=Path, help="Optional machine-readable JSON output path"
    )
    parser.add_argument(
        "--toml-output", type=Path, help="Optional portfolio-symbol TOML fragment path"
    )
    args = parser.parse_args()
    if not math.isfinite(args.option_field_timeout) or args.option_field_timeout <= 0:
        parser.error("--option-field-timeout must be finite and positive")
    if args.ibkr:
        if args.config is None or not args.symbols:
            parser.error("--ibkr requires both --config and --symbols")
        policy = AnalysisPolicy(target_dte=args.target_dte, capital=args.capital)
        symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
        if not symbols:
            parser.error("--symbols must contain at least one symbol")
        risk_groups: dict[str, str] = {}
        if args.risk_groups:
            for mapping in args.risk_groups.split(","):
                if "=" not in mapping:
                    parser.error("--risk-groups entries must use SYMBOL=GROUP")
                symbol, group = mapping.split("=", 1)
                if not symbol.strip() or not group.strip():
                    parser.error("--risk-groups entries must use SYMBOL=GROUP")
                risk_groups[symbol.strip().upper()] = group.strip()
        try:
            candidates = asyncio.run(
                _collect_from_ibkr(
                    args.config,
                    symbols,
                    policy,
                    risk_groups,
                    args.market_data_type,
                    args.option_field_timeout,
                )
            )
        except CandidateAnalysisError as exc:
            label = "错误" if args.language == "zh" else "error"
            parser.exit(
                2, f"{label}: {_translate_cli_error(str(exc), args.language)}\n"
            )
        default_output = args.config.with_name("candidate-analysis.md")
    else:
        assert args.input is not None
        candidates, policy = load_input(args.input)
        if args.capital is not None:
            policy = AnalysisPolicy(**{**asdict(policy), "capital": args.capital})
        default_output = args.input.with_suffix(".analysis.md")
    try:
        report = analyze_candidates(candidates, policy)
    except CandidateAnalysisError as exc:
        label = "错误" if args.language == "zh" else "error"
        if args.json_output:
            _write_json(
                args.json_output,
                {
                    "status": "analysis_failed",
                    "error": str(exc),
                    "policy": asdict(policy),
                    "assessments": [item.to_dict() for item in exc.assessments],
                    "observations": [asdict(item) for item in exc.observations],
                    "recommendations": [],
                },
            )
        parser.exit(2, f"{label}: {_format_analysis_error(exc, args.language)}\n")
    markdown_path = args.output or default_output
    markdown = render_markdown(report, language=args.language)
    markdown_path.write_text(markdown, encoding="utf-8")
    if args.json_output:
        _write_json(args.json_output, report.to_dict())
    if args.toml_output:
        args.toml_output.write_text(render_toml(report), encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    cli()
