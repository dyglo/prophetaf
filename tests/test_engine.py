from __future__ import annotations

import json

import pytest

from hedge_fund.backtesting.engine import BacktestEngine, BacktestResult, Trade, serialize_backtest_result
from hedge_fund.backtesting.reporter import format_backtest_report
from hedge_fund.tools.run_backtest import execute_backtest, run_backtest


def _candle(
    index: int,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    pair: str = "EURUSD",
    granularity: str = "H1",
) -> dict[str, object]:
    return {
        "time": f"2026-01-{index + 1:02d}T00:00:00",
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000 + index,
        "pair": pair,
        "granularity": granularity,
    }


def _signal(candle_time: str, direction: str = "long") -> dict[str, object]:
    return {
        "type": "fvg",
        "direction": direction,
        "zone_high": 1.1000,
        "zone_low": 1.0990,
        "candle_time": candle_time,
        "pair": "EURUSD",
        "granularity": "H1",
        "strength": 0.9,
    }


def test_engine_returns_backtest_result_shape() -> None:
    candles = [
        _candle(0, open_price=1.1000, high=1.1010, low=1.0990, close=1.1005),
        _candle(1, open_price=1.1005, high=1.1010, low=1.0990, close=1.1000),
        _candle(2, open_price=1.1000, high=1.1045, low=1.1001, close=1.1040),
    ]
    engine = BacktestEngine(
        candle_loader=lambda pair, granularity, from_date, to_date: candles,
        signal_detector=lambda observed: [_signal(str(candles[0]["time"]))] if len(observed) >= 1 else [],
    )

    result = engine.run("EURUSD", "H1", "2026-01-01", "2026-01-31")

    assert isinstance(result, BacktestResult)
    assert result.total_trades == 1
    assert result.pair == "EURUSD"
    assert result.granularity == "H1"
    serialized = serialize_backtest_result(result)
    assert serialized["trades"][0]["direction"] == "LONG"
    assert "tp1_hit" in serialized["trades"][0]


def test_no_lookahead_detector_only_receives_past_candles() -> None:
    candles = [
        _candle(0, open_price=1.1000, high=1.1010, low=1.0990, close=1.1005),
        _candle(1, open_price=1.1005, high=1.1015, low=1.0995, close=1.1008),
        _candle(2, open_price=1.1008, high=1.1020, low=1.1000, close=1.1015),
        _candle(3, open_price=1.1015, high=1.1030, low=1.1010, close=1.1020),
    ]
    observed_lengths: list[int] = []
    observed_last_times: list[str] = []

    def detector(observed: list[dict[str, object]]) -> list[dict[str, object]]:
        observed_lengths.append(len(observed))
        observed_last_times.append(str(observed[-1]["time"]))
        return []

    engine = BacktestEngine(
        candle_loader=lambda pair, granularity, from_date, to_date: candles,
        signal_detector=detector,
    )

    engine.run("EURUSD", "H1", "2026-01-01", "2026-01-31")

    assert observed_lengths == [1, 2, 3]
    assert observed_last_times == [candles[0]["time"], candles[1]["time"], candles[2]["time"]]


@pytest.mark.parametrize(
    ("candles", "expected_rr"),
    [
        (
            [
                _candle(0, open_price=1.1000, high=1.1010, low=1.0990, close=1.1005),
                _candle(1, open_price=1.1005, high=1.1008, low=1.0985, close=1.0990),
            ],
            -1.0,
        ),
        (
            [
                _candle(0, open_price=1.1000, high=1.1010, low=1.0990, close=1.1005),
                _candle(1, open_price=1.1005, high=1.1025, low=1.0995, close=1.1020),
                _candle(2, open_price=1.1020, high=1.1022, low=1.0998, close=1.1000),
            ],
            1.0,
        ),
            (
                [
                    _candle(0, open_price=1.1000, high=1.1010, low=1.0990, close=1.1005),
                    _candle(1, open_price=1.1005, high=1.1005, low=1.0995, close=1.1002),
                    _candle(2, open_price=1.1002, high=1.1045, low=1.1001, close=1.1040),
                ],
                2.5,
            ),
    ],
)
def test_trade_pnl_matches_scale_out_policy(candles: list[dict[str, object]], expected_rr: float) -> None:
    engine = BacktestEngine(
        candle_loader=lambda pair, granularity, from_date, to_date: candles,
        signal_detector=lambda observed: [_signal(str(candles[0]["time"]))] if len(observed) >= 1 else [],
    )

    result = engine.run("EURUSD", "H1", "2026-01-01", "2026-01-31")

    assert result.total_trades == 1
    assert result.trades[0].realized_rr == pytest.approx(expected_rr)
    assert result.total_pnl == pytest.approx(100.0 * expected_rr)


def test_max_drawdown_uses_closed_equity_curve() -> None:
    engine = BacktestEngine(lambda *args: [], lambda observed: [])

    max_drawdown = engine._max_drawdown([10000.0, 10100.0, 9950.0, 10300.0, 10050.0])  # noqa: SLF001

    assert max_drawdown == pytest.approx(250.0)


def test_empty_signals_returns_zero_trades_without_crash() -> None:
    candles = [
        _candle(0, open_price=1.1000, high=1.1010, low=1.0990, close=1.1005),
        _candle(1, open_price=1.1005, high=1.1015, low=1.0995, close=1.1008),
    ]
    engine = BacktestEngine(
        candle_loader=lambda pair, granularity, from_date, to_date: candles,
        signal_detector=lambda observed: [],
    )

    result = engine.run("EURUSD", "H1", "2026-01-01", "2026-01-31")

    assert result.total_trades == 0
    assert result.total_pnl == 0.0
    assert result.max_drawdown == 0.0
    assert result.avg_rr == 0.0


def test_format_backtest_report_returns_non_empty_string() -> None:
    result = BacktestResult(
        trades=[
            Trade(
                pair="EURUSD",
                granularity="H1",
                direction="LONG",
                signal_time="2026-01-01T00:00:00",
                entry_time="2026-01-02T00:00:00",
                exit_time="2026-01-03T00:00:00",
                entry_price=1.1000,
                stop_loss=1.1000,
                tp1=1.1020,
                tp2=1.1030,
                exit_price=1.1030,
                risk_amount=100.0,
                position_size=100000.0,
                pnl=250.0,
                realized_rr=2.5,
                exit_reason="tp2",
                tp1_hit=True,
                tp2_hit=True,
            )
        ],
        win_rate=100.0,
        total_pnl=250.0,
        max_drawdown=0.0,
        total_trades=1,
        avg_rr=2.5,
        pair="EURUSD",
        granularity="H1",
        from_date="2026-01-01",
        to_date="2026-01-31",
    )

    report = format_backtest_report(result)

    assert report
    assert "Best Trades" in report
    assert "Worst Trades" in report


def test_execute_backtest_handles_empty_dates() -> None:
    payload = execute_backtest(pair="EURUSD")

    assert payload["ok"] is True
    assert payload["backtest"]["pair"] == "EURUSD"
    assert payload["backtest"]["from_date"]
    assert payload["backtest"]["to_date"]


def test_run_backtest_tool_handles_empty_dates() -> None:
    raw_payload = run_backtest.invoke({"pair": "EURUSD"})
    payload = json.loads(raw_payload)

    assert payload["ok"] is True
    assert payload["report"]
