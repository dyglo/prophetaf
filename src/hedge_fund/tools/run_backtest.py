from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from langchain.tools import tool

from analysis.signals import detect_all_signals
from hedge_fund.backtesting.engine import BacktestEngine, serialize_backtest_result
from hedge_fund.backtesting.reporter import format_backtest_report
from tools.data_cache import load_candles


def _normalize_date_range(granularity: str, from_date: str, to_date: str, candle_count: int = 200) -> tuple[str, str]:
    step = _granularity_step(granularity)
    today = datetime.now(tz=UTC).date()
    resolved_to = datetime.fromisoformat(to_date).date() if to_date else today

    if from_date:
        resolved_from = datetime.fromisoformat(from_date).date()
    else:
        total_span = step * max(candle_count - 1, 0)
        resolved_from = (datetime.combine(resolved_to, datetime.min.time()) - total_span).date()

    if not to_date:
        total_span = step * max(candle_count - 1, 0)
        resolved_to = (datetime.combine(resolved_from, datetime.min.time()) + total_span).date()

    return resolved_from.isoformat(), resolved_to.isoformat()


def _granularity_step(granularity: str) -> timedelta:
    normalized = (granularity or "H1").strip().upper()
    mapping = {
        "M5": timedelta(minutes=5),
        "M15": timedelta(minutes=15),
        "M30": timedelta(minutes=30),
        "H1": timedelta(hours=1),
        "H4": timedelta(hours=4),
        "D1": timedelta(days=1),
    }
    return mapping.get(normalized, timedelta(hours=1))


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
    resolved_from_date, resolved_to_date = _normalize_date_range(granularity, from_date, to_date)
    engine = BacktestEngine(
        candle_loader=candle_loader or load_candles,
        signal_detector=signal_detector or detect_all_signals,
    )
    result = engine.run(
        pair=pair,
        granularity=granularity,
        from_date=resolved_from_date,
        to_date=resolved_to_date,
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
