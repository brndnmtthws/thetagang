from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from ib_async import IB, Option, OptionComputation, Ticker

import thetagang.candidate_analysis as candidate_analysis
from thetagang.candidate_analysis import (
    AnalysisPolicy,
    CandidateAnalysisError,
    CandidateMetrics,
    IBKRCandidateDataProvider,
    analyze_candidates,
    load_input,
    render_markdown,
    render_toml,
)


class FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, *args) -> None:
        for handler in list(self.handlers):
            handler(*args)


class FakeIB:
    def __init__(self) -> None:
        self.errorEvent = FakeEvent()
        self.connected_with = None
        self.disconnected = False

    async def connectAsync(self, **kwargs) -> None:
        self.connected_with = kwargs

    def disconnect(self) -> None:
        self.disconnected = True


def candidate(
    symbol: str,
    *,
    group: str = "equity",
    spread: float = 0.01,
    volume: float = 1_000,
    open_interest: float = 5_000,
    dte: int = 45,
    earnings_days: int | None = 60,
    price: float = 100.0,
) -> CandidateMetrics:
    return CandidateMetrics(
        symbol=symbol,
        price=price,
        bid_ask_pct=spread,
        option_volume=volume,
        open_interest=open_interest,
        dte=dte,
        earnings_days=earnings_days,
        implied_volatility=0.20,
        realized_volatility=0.18,
        option_yield=0.03,
        risk_group=group,
    )


def test_analyzer_allocates_one_hundred_percent_with_caps() -> None:
    report = analyze_candidates(
        [
            candidate("AAA", group="equity_us"),
            candidate("BBB", group="equity_us", spread=0.02),
            candidate("CCC", group="international", spread=0.015),
            candidate("DDD", group="bonds", spread=0.02),
        ],
        AnalysisPolicy(max_single_weight=0.30, max_group_weight=0.50),
    )

    assert sum(item.weight for item in report.recommendations) == pytest.approx(1.0)
    assert all(item.weight <= 0.30 + 1e-9 for item in report.recommendations)
    assert report.rejected_symbols == ()


def test_hard_filters_explain_rejection() -> None:
    report = analyze_candidates(
        [
            candidate("GOOD", group="a"),
            candidate("WIDE", group="b", spread=0.20),
            candidate("EVENT", group="c", earnings_days=3),
            candidate("STALE", group="d", open_interest=1),
        ],
        AnalysisPolicy(
            max_single_weight=1.0,
            max_group_weight=1.0,
            min_option_volume=100,
            min_open_interest=100,
        ),
    )

    assert set(report.rejected_symbols) == {"WIDE", "EVENT", "STALE"}
    by_symbol = {item.symbol: item for item in report.assessments}
    assert "bid-ask spread" in by_symbol["WIDE"].reasons[0]
    assert "earnings event" in by_symbol["EVENT"].reasons[0]
    assert "open interest" in by_symbol["STALE"].reasons[0]


def test_contract_size_warning_is_reported() -> None:
    report = analyze_candidates(
        [
            candidate("AAA", group="a", price=500),
            candidate("BBB", group="b", price=100),
            candidate("CCC", group="c", price=100),
            candidate("DDD", group="d", price=100),
        ],
        AnalysisPolicy(
            capital=50_000,
            margin_usage=0.5,
            max_single_weight=0.30,
            max_group_weight=0.50,
        ),
    )

    assert any("AAA: one contract" in warning for warning in report.warnings)
    assert report.recommendations[0].target_value is not None


def test_input_loader_and_markdown_output(tmp_path) -> None:
    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps(
            {
                "policy": {
                    "max_single_weight": 0.30,
                    "max_group_weight": 0.50,
                },
                "candidates": [
                    {
                        "symbol": "AAA",
                        "price": 100,
                        "bid_ask_pct": 0.01,
                        "option_volume": 1_000,
                        "open_interest": 5_000,
                        "dte": 45,
                        "risk_group": "a",
                    },
                    {
                        "symbol": "BBB",
                        "price": 100,
                        "bid_ask_pct": 0.01,
                        "option_volume": 1_000,
                        "open_interest": 5_000,
                        "dte": 45,
                        "risk_group": "b",
                    },
                    {
                        "symbol": "CCC",
                        "price": 100,
                        "bid_ask_pct": 0.01,
                        "option_volume": 1_000,
                        "open_interest": 5_000,
                        "dte": 45,
                        "risk_group": "c",
                    },
                    {
                        "symbol": "DDD",
                        "price": 100,
                        "bid_ask_pct": 0.01,
                        "option_volume": 1_000,
                        "open_interest": 5_000,
                        "dte": 45,
                        "risk_group": "d",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    candidates, policy = load_input(path)
    report = analyze_candidates(candidates, policy)
    markdown = render_markdown(report)
    assert "# 候选标的分析与权重建议" in markdown
    assert "## 推荐权重" in markdown
    assert "综合评分" in markdown
    assert "[symbols.AAA]" in markdown
    assert "[symbols.AAA]" in render_toml(report)
    assert "## Recommended weights" in render_markdown(report, language="en")


def test_duplicate_symbols_and_infeasible_caps_fail() -> None:
    with pytest.raises(CandidateAnalysisError, match="unique"):
        analyze_candidates([candidate("AAA"), candidate("AAA", group="b")])

    with pytest.raises(CandidateAnalysisError, match="cannot reach 100%"):
        analyze_candidates(
            [candidate("AAA", group="a"), candidate("BBB", group="a")],
            AnalysisPolicy(max_single_weight=0.30, max_group_weight=0.50),
        )


def test_no_eligible_candidates_include_rejection_details() -> None:
    with pytest.raises(
        CandidateAnalysisError, match="SPY: bid-ask spread exceeds"
    ) as exc:
        analyze_candidates(
            [candidate("SPY", spread=0.20, volume=1, open_interest=1)],
        )

    assert "option volume is below 100" in str(exc.value)
    assert "open interest is below 500" in str(exc.value)


def test_cli_error_is_translated_to_chinese() -> None:
    message = (
        "No candidate passed the hard eligibility filters. Rejections: "
        "SPY: bid-ask spread exceeds 5.0% || option volume is below 100 | "
        "QQQ: open interest is below 500"
    )

    translated = candidate_analysis._translate_cli_error(message, "zh")

    assert translated.startswith("没有候选标的通过硬性筛选。拒绝原因：")
    assert "SPY：买卖价差超过 5.0%" in translated
    assert "QQQ：未平仓量低于 500" in translated


def test_ibkr_option_metrics_use_conservative_liquidity_and_spread() -> None:
    put = Ticker(contract=Option("AAA", "20991231", 100, "P", "SMART"))
    (
        put.bid,
        put.ask,
        put.bidSize,
        put.askSize,
        put.volume,
        put.putOpenInterest,
        put.impliedVolatility,
    ) = (
        1.0,
        1.2,
        10,
        10,
        1_000,
        4_000,
        0.20,
    )
    call = Ticker(contract=Option("AAA", "20991231", 100, "C", "SMART"))
    (
        call.bid,
        call.ask,
        call.bidSize,
        call.askSize,
        call.volume,
        call.callOpenInterest,
        call.impliedVolatility,
    ) = (
        1.5,
        1.8,
        10,
        10,
        2_000,
        6_000,
        0.24,
    )
    put.modelGreeks = OptionComputation(0, 0.20, -0.5, 1.1, 0, 0.1, 0.2, -0.1, 100)
    call.modelGreeks = OptionComputation(0, 0.24, 0.5, 1.65, 0, 0.1, 0.2, -0.1, 100)

    metrics = IBKRCandidateDataProvider._option_metrics([put, call], 100, 45)

    assert metrics["option_volume"] == 1_000
    assert metrics["open_interest"] == 4_000
    assert metrics["bid_ask_pct"] == pytest.approx(0.3 / 1.65)
    assert metrics["implied_volatility"] == pytest.approx(0.22)


@pytest.mark.parametrize(
    ("configured", "override", "expected"),
    [(3, None, 3), (1, 4, 4)],
)
def test_ibkr_collection_applies_market_data_type(
    tmp_path, monkeypatch, configured, override, expected
) -> None:
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text(
        f"""
[runtime.account]
number = ""
market_data_type = {configured}

[runtime.watchdog]
host = "127.0.0.1"
port = 7497
clientId = 12
""",
        encoding="utf-8",
    )
    fake_ib = FakeIB()
    selected = []

    class FakeIBKR:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def set_market_data_type(self, value) -> None:
            selected.append(value)

    async def fake_collect(_provider, symbols):
        fake_ib.errorEvent.emit(
            7,
            10090,
            "Part of requested market data is not subscribed",
            Option("AAA", "20991231", 100, "P", "SMART"),
        )
        return [candidate(symbol) for symbol in symbols]

    monkeypatch.setattr(candidate_analysis, "IB", lambda: fake_ib)
    monkeypatch.setattr(candidate_analysis, "IBKR", FakeIBKR)
    monkeypatch.setattr(candidate_analysis, "collect_candidates", fake_collect)

    results = asyncio.run(
        candidate_analysis._collect_from_ibkr(
            config_path,
            ["AAA"],
            AnalysisPolicy(),
            market_data_type_override=override,
        )
    )

    assert [item.symbol for item in results] == ["AAA"]
    assert selected == [expected]
    assert any("IBKR 10090 (reqId=7)" in note for note in results[0].notes)
    assert fake_ib.disconnected
    assert fake_ib.errorEvent.handlers == []


def test_ibkr_collection_explains_competing_live_session(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "thetagang.toml"
    config_path.write_text(
        """
[runtime.account]
number = ""
market_data_type = 1
""",
        encoding="utf-8",
    )
    fake_ib = FakeIB()

    class FakeIBKR:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def set_market_data_type(self, _value) -> None:
            pass

    async def competing_session(_provider, _symbols):
        fake_ib.errorEvent.emit(
            4,
            10197,
            "No market data during competing live session",
            None,
        )
        await asyncio.Event().wait()

    monkeypatch.setattr(candidate_analysis, "IB", lambda: fake_ib)
    monkeypatch.setattr(candidate_analysis, "IBKR", FakeIBKR)
    monkeypatch.setattr(candidate_analysis, "collect_candidates", competing_session)

    with pytest.raises(CandidateAnalysisError, match="--market-data-type 3"):
        asyncio.run(
            candidate_analysis._collect_from_ibkr(
                config_path,
                ["AAA"],
                AnalysisPolicy(),
            )
        )

    assert fake_ib.disconnected
    assert fake_ib.errorEvent.handlers == []


def test_market_data_type_validation() -> None:
    assert candidate_analysis._resolve_market_data_type(1, 3) == 3
    with pytest.raises(CandidateAnalysisError, match="integer from 1 to 4"):
        candidate_analysis._resolve_market_data_type("live")


def option_ticker(right: str) -> Ticker:
    ticker = Ticker(contract=Option("AAA", "20991231", 100, right, "SMART"))
    ticker.bid, ticker.ask = 1.0, 1.02
    ticker.bidSize = ticker.askSize = 10
    ticker.volume = 1000
    ticker.putOpenInterest = ticker.callOpenInterest = 5000
    return ticker


@pytest.mark.parametrize("unset", [None, float("nan"), float("inf"), -1.0])
def test_missing_liquidity_is_not_zero_or_a_threshold_rejection(unset) -> None:
    put, call = option_ticker("P"), option_ticker("C")
    put.volume = unset
    put.putOpenInterest = unset
    raw = IBKRCandidateDataProvider._option_metrics([put, call], 100, 45)
    metrics = CandidateMetrics(symbol="AAA", price=100, dte=45, **raw)
    result = candidate_analysis.assess_candidate(metrics, AnalysisPolicy())

    assert result.status == "data_insufficient"
    assert not result.eligible
    assert result.reasons == ()
    assert set(result.missing_data) == {"option_volume", "open_interest"}
    assert raw["option_samples"][0]["volume"] is None
    assert raw["option_volume"] is None


def test_true_zero_is_preserved_without_fallback_to_generic_fields() -> None:
    put, call = option_ticker("P"), option_ticker("C")
    put.volume = put.putOpenInterest = 0
    put.putVolume = put.openInterest = 50000
    raw = IBKRCandidateDataProvider._option_metrics([put, call], 100, 45)
    result = candidate_analysis.assess_candidate(
        CandidateMetrics(symbol="AAA", price=100, dte=45, **raw), AnalysisPolicy()
    )

    assert result.status == "rejected"
    assert result.missing_data == ()
    assert raw["option_volume"] == raw["open_interest"] == 0
    assert len(result.reasons) == 2


@pytest.mark.asyncio
async def test_contract_iv_uses_real_ib_async_option_computation_callbacks() -> None:
    ib = IB()  # Offline wrapper only; no broker connection.
    tickers = []
    for req_id, right, delta in [(1, "P", -0.4), (2, "C", 0.4)]:
        contract = Option("AAA", "20991231", 100, right, "SMART", conId=req_id)
        ticker = ib.wrapper.startTicker(req_id, contract, "mktData")
        ib.wrapper.tickOptionComputation(
            req_id, 13, 0, 0.22, delta, 1.0, 0.0, 0.01, 0.1, -0.1, 100
        )
        ib.wrapper.tickSize(req_id, 8, 200)
        ib.wrapper.tickSize(req_id, 28 if right == "P" else 27, 700)
        ticker.bid, ticker.ask = 1, 1.02
        # Generic 30-day IV must never override this contract's IV.
        ticker.impliedVolatility = 0.99
        tickers.append(ticker)
    raw = IBKRCandidateDataProvider._option_metrics(tickers, 100, 45)

    assert raw["implied_volatility"] == pytest.approx(0.22)
    assert raw["option_volume"] == 200
    assert raw["open_interest"] == 700
    assert raw["notes"] == ()
    assert raw["option_samples"][0]["iv_source"] == "modelGreeks"


def test_bid_iv_fallback_and_missing_model_iv_do_not_imply_missing_delta() -> None:
    put, call = option_ticker("P"), option_ticker("C")
    for ticker in (put, call):
        ticker.modelGreeks = OptionComputation(0, None, 0.4, 1, 0, 0.1, 0.1, -0.1, 100)
        ticker.bidGreeks = OptionComputation(0, 0.3, 0.4, 1, 0, 0.1, 0.1, -0.1, 100)
    raw = IBKRCandidateDataProvider._option_metrics([put, call], 100, 45)
    assert raw["implied_volatility"] == 0.3
    assert raw["notes"] == ()
    for ticker in (put, call):
        ticker.bidGreeks = None
        ticker.impliedVolatility = 0.99
    raw = IBKRCandidateDataProvider._option_metrics([put, call], 100, 45)
    assert raw["implied_volatility"] is None
    assert raw["notes"] == (
        "contract implied volatility is unavailable for some sampled options",
    )


def test_missing_one_quote_blocks_allocation_even_with_other_leg_liquid() -> None:
    put, call = option_ticker("P"), option_ticker("C")
    put.bid = float("nan")
    raw = IBKRCandidateDataProvider._option_metrics([put, call], 100, 45)
    result = candidate_analysis.assess_candidate(
        CandidateMetrics(symbol="AAA", price=100, dte=45, **raw), AnalysisPolicy()
    )
    assert result.status == "data_insufficient"
    assert result.missing_data == ("bid_ask_pct",)


def test_cli_failure_exports_evidence_and_separates_warnings(
    tmp_path, monkeypatch, capsys
) -> None:
    put, call = option_ticker("P"), option_ticker("C")
    put.putOpenInterest = float("nan")
    raw = IBKRCandidateDataProvider._option_metrics([put, call], 100, 45)
    observation = CandidateMetrics(symbol="AAA", price=100, dte=45, **raw)
    # Also verify that legacy provider warnings are deduplicated.
    observation = replace(
        observation,
        notes=(
            *observation.notes,
            "earnings date was not supplied by the data provider",
        ),
    )
    monkeypatch.setattr(
        candidate_analysis, "load_input", lambda _: ([observation], AnalysisPolicy())
    )
    output = tmp_path / "report.md"
    output.write_text("previous report", encoding="utf-8")
    diagnostic = tmp_path / "diagnostics.json"
    toml_output = tmp_path / "symbols.toml"
    monkeypatch.setattr(
        "sys.argv",
        [
            "analyze",
            "--input",
            "unused.json",
            "--output",
            str(output),
            "--json-output",
            str(diagnostic),
            "--toml-output",
            str(toml_output),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        candidate_analysis.cli()
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "AAA: 数据不足" in error
    assert "OI=缺失" in error
    assert "硬性拒绝" not in error
    assert error.count("未提供财报日期") == 1
    assert "警告（不单独导致拒绝）" in error
    payload = json.loads(diagnostic.read_text())
    assert payload["status"] == "analysis_failed"
    assert payload["recommendations"] == []
    assert payload["observations"][0]["option_samples"][0]["open_interest"] is None
    assert payload["assessments"][0]["status"] == "data_insufficient"
    assert output.read_text() == "previous report"
    assert not toml_output.exists()


@pytest.mark.asyncio
async def test_provider_waits_for_volume_and_keeps_requested_market_data_type() -> None:
    from types import SimpleNamespace

    from thetagang.ibkr import TickerField

    broker = Mock()
    stock = option_ticker("P")
    broker.get_ticker_for_stock = AsyncMock(return_value=stock)
    broker.get_chains_for_contract = AsyncMock(
        return_value=[
            SimpleNamespace(
                tradingClass="AAA",
                expirations=["20991231"],
                strikes=[1.0],
            )
        ]
    )
    broker.qualify_contracts = AsyncMock(side_effect=lambda *contracts: list(contracts))
    broker.get_tickers_for_contracts = AsyncMock(
        return_value=[option_ticker("P"), option_ticker("C")]
    )
    broker.request_historical_data = AsyncMock(return_value=[])
    provider = IBKRCandidateDataProvider(
        broker,
        requested_market_data_type=2,
        option_field_timeout=30,
    )
    result = await provider.fetch("AAA")
    assert result.requested_market_data_type == 2
    kwargs = broker.get_tickers_for_contracts.call_args.kwargs
    assert TickerField.VOLUME in kwargs["optional_fields"]
    assert kwargs["field_timeout"] == 30
