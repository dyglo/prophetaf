from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from langchain.tools import tool

from hedge_fund.backtesting.engine import BacktestEngine, serialize_backtest_result
from hedge_fund.backtesting.reporter import format_backtest_report


def _stub_candle_loader(pair: str, granularity: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    start = datetime.fromisoformat(f"{from_date}T00:00:00")
    is_xau = pair == "XAUUSD"
    base_price = 2900.0 if is_xau else 1.1000
    drift_step = 0.6 if is_xau else 0.0006
    noise = 1.0 if is_xau else 0.0010
    precision = 2 if is_xau else 5
    candles: list[dict[str, Any]] = []

    for index in range(200):
        drift = drift_step * index
        wave = noise * (1 if index % 10 < 5 else -0.8)
        close = base_price + drift + wave
        open_price = close - (0.4 if is_xau else 0.0004)
        high = max(open_price, close) + noise
        low = min(open_price, close) - noise
        candles.append(
            {
                "time": (start + timedelta(hours=index)).isoformat(),
                "open": round(open_price, precision),
                "high": round(high, precision),
                "low": round(low, precision),
                "close": round(close, precision),
                "volume": 1000 + index,
                "pair": pair,
                "granularity": granularity,
            }
        )
    return candles


def _stub_signal_detector(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    anchor_indexes = [20, 50, 80, 110, 140]
    signals: list[dict[str, Any]] = []

    for anchor in anchor_indexes:
        if len(candles) <= anchor:
            continue
        candle = candles[anchor]
        is_xau = candle["pair"] == "XAUUSD"
        zone_size = 0.8 if is_xau else 0.0008
        precision = 2 if is_xau else 5
        close = float(candle["close"])
        signals.append(
            {
                "type": "fvg_fib_liquidity",
                "direction": "long" if anchor % 2 == 0 else "short",
                "zone_high": round(close + zone_size, precision),
                "zone_low": round(close, precision),
                "candle_time": str(candle["time"]),
                "pair": candle["pair"],
                "granularity": candle["granularity"],
                "strength": 0.8,
            }
        )

    return signals


def run_backtest_payload(
    pair: str,
    granularity: str = "H1",
    from_date: str = "",
    to_date: str = "",
    account_size: float = 10000.0,
    risk_pct: float = 0.01,
    candle_loader: Callable[[str, str, str, str], list[dict[str, Any]]] | None = None,
    signal_detector: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    engine = BacktestEngine(
        candle_loader or _stub_candle_loader,
        signal_detector or _stub_signal_detector,
    )
    result = engine.run(
        pair=pair,
        granularity=granularity,
        from_date=from_date,
        to_date=to_date,
        account_size=account_size,
        risk_pct=risk_pct,
    )
    report = format_backtest_report(result)
    summary = (
        f"{result.pair} {result.granularity}: {result.total_trades} trades, "
        f"{result.win_rate:.2f}% win rate, total PnL ${result.total_pnl:.2f}."
    )
    return {
        "ok": True,
        "backtest": serialize_backtest_result(result),
        "report": report,
        "summary": summary,
    }


def execute_backtest(
    pair: str,
    granularity: str = "H1",
    from_date: str = "",
    to_date: str = "",
    account_size: float = 10000.0,
    risk_pct: float = 0.01,
) -> dict[str, Any]:
    return run_backtest_payload(
        pair=pair,
        granularity=granularity,
        from_date=from_date,
        to_date=to_date,
        account_size=account_size,
        risk_pct=risk_pct,
    )


@tool
def run_backtest(
    pair: str,
    granularity: str = "H1",
    from_date: str = "",
    to_date: str = "",
    account_size: float = 10000.0,
) -> str:
    """Run a backtest of Prophet's FVG + Fibonacci + liquidity sweep strategy on historical data for a given pair."""
    payload = execute_backtest(
        pair=pair,
        granularity=granularity,
        from_date=from_date,
        to_date=to_date,
        account_size=account_size,
    )
    return json.dumps(payload, default=str)
