from __future__ import annotations

from typing import Literal, TypedDict


class CandleRow(TypedDict):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    pair: str
    granularity: str


class Signal(TypedDict):
    type: Literal["fvg", "fibonacci", "liquidity_sweep"]
    direction: Literal["bullish", "bearish"]
    zone_high: float
    zone_low: float
    candle_time: str
    pair: str
    granularity: str
    strength: float


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _average_range(candles: list[CandleRow], end_index: int, window: int = 20) -> float:
    start_index = max(0, end_index - window + 1)
    segment = candles[start_index : end_index + 1]
    if not segment:
        return 0.0
    total_range = sum(max(candle["high"] - candle["low"], 0.0) for candle in segment)
    return total_range / len(segment)


def detect_fvg(candles: list[CandleRow]) -> list[Signal]:
    if len(candles) < 3:
        return []

    signals: list[Signal] = []
    for index in range(1, len(candles) - 1):
        previous = candles[index - 1]
        next_candle = candles[index + 1]

        if previous["high"] < next_candle["low"]:
            zone_low = previous["high"]
            zone_high = next_candle["low"]
            direction: Literal["bullish", "bearish"] = "bullish"
        elif previous["low"] > next_candle["high"]:
            zone_low = next_candle["high"]
            zone_high = previous["low"]
            direction = "bearish"
        else:
            continue

        gap_size = zone_high - zone_low
        average_range = _average_range(candles, index + 1)
        strength = 0.0 if average_range <= 0.0 else _clamp(gap_size / average_range)

        signals.append(
            Signal(
                type="fvg",
                direction=direction,
                zone_high=zone_high,
                zone_low=zone_low,
                candle_time=next_candle["time"],
                pair=next_candle["pair"],
                granularity=next_candle["granularity"],
                strength=strength,
            )
        )

    return signals
