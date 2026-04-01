from __future__ import annotations

from typing import Literal

from analysis.fvg_detector import CandleRow, Signal, detect_fvg


FIBONACCI_LEVELS = (0.5, 0.786)


def _zones_overlap(zone_low: float, zone_high: float, other_low: float, other_high: float) -> bool:
    return zone_low <= other_high and other_low <= zone_high


def detect_fibonacci_zones(candles: list[CandleRow], lookback: int = 50) -> list[Signal]:
    if len(candles) < max(lookback, 3):
        return []

    fvg_signals = detect_fvg(candles)
    signals: list[Signal] = []

    for index in range(lookback - 1, len(candles)):
        window_start = index - lookback + 1
        window = candles[window_start : index + 1]
        current = candles[index]

        high_offset, swing_high_candle = max(enumerate(window), key=lambda item: item[1]["high"])
        low_offset, swing_low_candle = min(enumerate(window), key=lambda item: item[1]["low"])

        if high_offset == low_offset:
            continue

        swing_high = swing_high_candle["high"]
        swing_low = swing_low_candle["low"]
        move = swing_high - swing_low
        if move <= 0.0:
            continue

        if low_offset < high_offset:
            direction: Literal["bullish", "bearish"] = "bullish"
        else:
            direction = "bearish"

        level_zones: list[tuple[float, float, float]] = []
        touched_levels: list[tuple[float, float, float]] = []
        for ratio in FIBONACCI_LEVELS:
            if direction == "bullish":
                level = swing_high - (move * ratio)
            else:
                level = swing_low + (move * ratio)

            zone_buffer = abs(level) * 0.005
            zone_low = level - zone_buffer
            zone_high = level + zone_buffer
            level_zones.append((ratio, zone_low, zone_high))

            if current["low"] <= zone_high and current["high"] >= zone_low:
                touched_levels.append((ratio, zone_low, zone_high))

        if not touched_levels:
            continue

        eligible_fvgs = [
            signal
            for signal in fvg_signals
            if signal["direction"] == direction and signal["candle_time"] <= current["time"]
        ]
        aligned_levels = {
            ratio
            for ratio, zone_low, zone_high in level_zones
            if any(
                _zones_overlap(zone_low, zone_high, signal["zone_low"], signal["zone_high"])
                for signal in eligible_fvgs
            )
        }
        strength = 1.0 if len(aligned_levels) == len(FIBONACCI_LEVELS) else 0.6

        for _, zone_low, zone_high in touched_levels:
            signals.append(
                Signal(
                    type="fibonacci",
                    direction=direction,
                    zone_high=zone_high,
                    zone_low=zone_low,
                    candle_time=current["time"],
                    pair=current["pair"],
                    granularity=current["granularity"],
                    strength=strength,
                )
            )

    return signals
