from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from analysis.fibonacci import detect_fibonacci_zones
from analysis.fvg_detector import detect_fvg
from analysis.liquidity import detect_liquidity_sweeps
from analysis.signals import detect_all_signals


def _time(index: int) -> str:
    return (datetime(2026, 3, 8, 7, 0, tzinfo=UTC) + timedelta(minutes=15 * index)).isoformat().replace("+00:00", "Z")


def _candle(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    pair: str = "EURUSD",
    granularity: str = "M15",
    volume: int = 1000,
) -> dict[str, object]:
    return {
        "time": _time(index),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "pair": pair,
        "granularity": granularity,
    }


def test_detect_fvg_returns_bullish_signal() -> None:
    candles = [
        _candle(0, 9.2, 10.0, 9.0, 9.8),
        _candle(1, 9.8, 10.2, 9.5, 10.0),
        _candle(2, 10.6, 11.0, 10.5, 10.8),
    ]

    result = detect_fvg(candles)

    assert len(result) == 1
    signal = result[0]
    assert signal["type"] == "fvg"
    assert signal["direction"] == "bullish"
    assert signal["zone_low"] == pytest.approx(10.0)
    assert signal["zone_high"] == pytest.approx(10.5)
    assert signal["candle_time"] == candles[2]["time"]
    assert signal["strength"] == pytest.approx(0.6818181818)


def test_detect_fvg_returns_bearish_signal() -> None:
    candles = [
        _candle(0, 11.3, 11.5, 10.9, 11.0),
        _candle(1, 11.0, 11.2, 10.7, 10.8),
        _candle(2, 10.3, 10.5, 10.0, 10.1),
    ]

    result = detect_fvg(candles)

    assert len(result) == 1
    signal = result[0]
    assert signal["direction"] == "bearish"
    assert signal["zone_low"] == pytest.approx(10.5)
    assert signal["zone_high"] == pytest.approx(10.9)
    assert signal["strength"] == pytest.approx(0.75)


def test_detect_fibonacci_zones_returns_bullish_signals_with_fvg_alignment() -> None:
    candles = [
        _candle(0, 102.5, 104.0, 101.5, 103.0),
        _candle(1, 103.0, 103.8, 102.0, 102.8),
        _candle(2, 102.8, 103.0, 100.0, 101.0),
        _candle(3, 104.8, 106.0, 104.6, 105.5),
        _candle(4, 108.8, 109.6, 108.5, 109.0),
        _candle(5, 109.0, 111.0, 108.8, 110.5),
        _candle(6, 110.8, 112.0, 110.4, 111.5),
        _candle(7, 111.5, 120.0, 111.0, 119.0),
        _candle(8, 109.8, 110.3, 104.2, 106.0),
    ]

    result = detect_fibonacci_zones(candles, lookback=7)
    current_signals = [signal for signal in result if signal["candle_time"] == candles[8]["time"]]

    assert len(current_signals) == 2
    assert {signal["direction"] for signal in current_signals} == {"bullish"}
    assert all(signal["strength"] == pytest.approx(1.0) for signal in current_signals)
    zones = sorted((signal["zone_low"], signal["zone_high"]) for signal in current_signals)
    assert zones[0][0] == pytest.approx(103.7586, rel=1e-4)
    assert zones[0][1] == pytest.approx(104.8014, rel=1e-4)
    assert zones[1][0] == pytest.approx(109.45, rel=1e-4)
    assert zones[1][1] == pytest.approx(110.55, rel=1e-4)


def test_detect_fibonacci_zones_returns_bearish_signals_without_alignment() -> None:
    candles = [
        _candle(0, 118.5, 119.0, 117.5, 118.0),
        _candle(1, 119.0, 120.0, 118.5, 119.2),
        _candle(2, 119.2, 119.5, 117.0, 117.5),
        _candle(3, 117.5, 118.6, 115.0, 115.5),
        _candle(4, 115.5, 117.2, 100.0, 101.0),
        _candle(5, 114.0, 116.0, 109.5, 115.5),
    ]

    result = detect_fibonacci_zones(candles, lookback=5)
    current_signals = [signal for signal in result if signal["candle_time"] == candles[5]["time"]]

    assert len(current_signals) == 2
    assert {signal["direction"] for signal in current_signals} == {"bearish"}
    assert all(signal["strength"] == pytest.approx(0.6) for signal in current_signals)
    zones = sorted((signal["zone_low"], signal["zone_high"]) for signal in current_signals)
    assert zones[0][0] == pytest.approx(109.45, rel=1e-4)
    assert zones[0][1] == pytest.approx(110.55, rel=1e-4)
    assert zones[1][0] == pytest.approx(115.1414, rel=1e-4)
    assert zones[1][1] == pytest.approx(116.2986, rel=1e-4)


def test_detect_liquidity_sweeps_returns_bullish_signal() -> None:
    candles = [
        _candle(0, 9.8, 10.0, 9.7, 9.9),
        _candle(1, 9.9, 10.1, 9.6, 9.8),
        _candle(2, 9.8, 10.0, 9.65, 9.75),
        _candle(3, 9.65, 9.8, 9.4, 9.75),
    ]

    result = detect_liquidity_sweeps(candles, lookback=3)

    assert len(result) == 1
    signal = result[0]
    assert signal["direction"] == "bullish"
    assert signal["zone_low"] == pytest.approx(9.56)
    assert signal["zone_high"] == pytest.approx(9.64)
    assert signal["strength"] == pytest.approx(0.5)


def test_detect_liquidity_sweeps_returns_bearish_signal() -> None:
    candles = [
        _candle(0, 10.1, 10.2, 9.9, 10.0),
        _candle(1, 10.0, 10.4, 9.95, 10.2),
        _candle(2, 10.2, 10.3, 10.0, 10.1),
        _candle(3, 10.3, 10.7, 10.2, 10.35),
    ]

    result = detect_liquidity_sweeps(candles, lookback=3)

    assert len(result) == 1
    signal = result[0]
    assert signal["direction"] == "bearish"
    assert signal["zone_low"] == pytest.approx(10.35)
    assert signal["zone_high"] == pytest.approx(10.45)
    assert signal["strength"] == pytest.approx(1.0)


def test_detect_all_signals_deduplicates_overlapping_fvg_zones_by_strength() -> None:
    candles = [
        _candle(0, 99.5, 100.0, 99.0, 99.8),
        _candle(1, 99.8, 100.4, 99.7, 100.0),
        _candle(2, 101.3, 101.8, 101.2, 101.6),
        _candle(3, 100.7, 101.0, 100.6, 100.9),
    ]

    result = detect_all_signals(candles)

    assert len(result) == 1
    signal = result[0]
    assert signal["type"] == "fvg"
    assert signal["zone_low"] == pytest.approx(100.0)
    assert signal["zone_high"] == pytest.approx(101.2)
    assert signal["strength"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("detector", "kwargs"),
    [
        (detect_fvg, {}),
        (detect_fibonacci_zones, {}),
        (detect_liquidity_sweeps, {}),
        (detect_all_signals, {}),
    ],
)
def test_signal_detectors_handle_empty_inputs(detector, kwargs) -> None:
    assert detector([], **kwargs) == []


@pytest.mark.parametrize(
    ("detector", "kwargs"),
    [
        (detect_fvg, {}),
        (detect_fibonacci_zones, {"lookback": 5}),
        (detect_liquidity_sweeps, {"lookback": 3}),
        (detect_all_signals, {}),
    ],
)
def test_signal_detectors_handle_short_inputs(detector, kwargs) -> None:
    candles = [
        _candle(0, 1.0, 1.1, 0.9, 1.0),
        _candle(1, 1.0, 1.2, 0.95, 1.1),
    ]

    assert detector(candles, **kwargs) == []
