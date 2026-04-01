from __future__ import annotations

from collections import defaultdict

from analysis.fibonacci import detect_fibonacci_zones
from analysis.fvg_detector import CandleRow, Signal, detect_fvg
from analysis.liquidity import detect_liquidity_sweeps


def _signal_key(signal: Signal) -> tuple[str, str, str, str]:
    return signal["type"], signal["direction"], signal["pair"], signal["granularity"]


def _better_signal(candidate: Signal, current: Signal) -> Signal:
    if candidate["strength"] > current["strength"]:
        return candidate
    if candidate["strength"] < current["strength"]:
        return current
    if candidate["candle_time"] < current["candle_time"]:
        return candidate
    return current


def _deduplicate(signals: list[Signal]) -> list[Signal]:
    grouped: dict[tuple[str, str, str, str], list[Signal]] = defaultdict(list)
    for signal in signals:
        grouped[_signal_key(signal)].append(signal)

    deduplicated: list[Signal] = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: (item["zone_low"], item["zone_high"], item["candle_time"]))
        cluster_best = ordered[0]
        cluster_high = ordered[0]["zone_high"]

        for signal in ordered[1:]:
            if signal["zone_low"] <= cluster_high:
                cluster_best = _better_signal(signal, cluster_best)
                cluster_high = max(cluster_high, signal["zone_high"])
                continue

            deduplicated.append(cluster_best)
            cluster_best = signal
            cluster_high = signal["zone_high"]

        deduplicated.append(cluster_best)

    return sorted(deduplicated, key=lambda item: item["candle_time"])


def detect_all_signals(candles: list[CandleRow]) -> list[Signal]:
    merged = []
    merged.extend(detect_fvg(candles))
    merged.extend(detect_fibonacci_zones(candles))
    merged.extend(detect_liquidity_sweeps(candles))
    if not merged:
        return []
    return _deduplicate(merged)
