from __future__ import annotations

from typing import Literal

from analysis.fvg_detector import CandleRow, Signal


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _wick_strength(candle: CandleRow, direction: Literal["bullish", "bearish"]) -> float:
    body = abs(candle["close"] - candle["open"])
    if direction == "bullish":
        wick = min(candle["open"], candle["close"]) - candle["low"]
    else:
        wick = candle["high"] - max(candle["open"], candle["close"])
    wick = max(wick, 0.0)
    return _clamp((wick / max(body, 1e-9)) / 5.0)


def detect_liquidity_sweeps(candles: list[CandleRow], lookback: int = 20) -> list[Signal]:
    if len(candles) <= lookback:
        return []

    signals: list[Signal] = []
    for index in range(lookback, len(candles)):
        current = candles[index]
        prior = candles[index - lookback : index]
        if not prior:
            continue

        prior_high = max(candle["high"] for candle in prior)
        prior_low = min(candle["low"] for candle in prior)
        candle_range = max(current["high"] - current["low"], 0.0)
        spread = max(candle_range * 0.1, 1e-9)

        if current["low"] < prior_low and current["close"] >= prior_low:
            direction: Literal["bullish", "bearish"] = "bullish"
            level = prior_low
        elif current["high"] > prior_high and current["close"] <= prior_high:
            direction = "bearish"
            level = prior_high
        else:
            continue

        signals.append(
            Signal(
                type="liquidity_sweep",
                direction=direction,
                zone_high=level + spread,
                zone_low=level - spread,
                candle_time=current["time"],
                pair=current["pair"],
                granularity=current["granularity"],
                strength=_wick_strength(current, direction),
            )
        )

    return signals
