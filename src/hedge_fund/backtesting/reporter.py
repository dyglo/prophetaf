from __future__ import annotations

from hedge_fund.backtesting.engine import BacktestResult, Trade


_RULE_WIDTH = 80


def format_backtest_report(result: BacktestResult) -> str:
    lines = [
        "PROPHET BACKTEST REPORT",
        _rule(),
        f"Pair: {result.pair:<12} Granularity: {result.granularity:<8} Period: {result.from_date} -> {result.to_date}",
        f"Trades: {result.total_trades:<10} Win rate: {result.win_rate:>6.2f}%   Avg RR: {result.avg_rr:>6.2f}R",
        f"Total PnL: ${result.total_pnl:>11.2f}   Max drawdown: ${result.max_drawdown:>8.2f}",
        _rule(),
        "Best Trades",
        _rule("-"),
    ]
    lines.extend(_format_trade_section(sorted(result.trades, key=lambda trade: trade.pnl, reverse=True)[:3]))
    lines.extend([
        _rule(),
        "Worst Trades",
        _rule("-"),
    ])
    lines.extend(_format_trade_section(sorted(result.trades, key=lambda trade: trade.pnl)[:3]))
    return "\n".join(lines).strip()


def _format_trade_section(trades: list[Trade]) -> list[str]:
    if not trades:
        return ["No trades recorded."]
    return [
        f"{trade.entry_time[:10]} {trade.direction:<5} {trade.exit_reason:<12} "
        f"RR {trade.realized_rr:>5.2f}R  PnL ${trade.pnl:>9.2f}"
        for trade in trades
    ]


def _rule(character: str = "=") -> str:
    return character * _RULE_WIDTH
