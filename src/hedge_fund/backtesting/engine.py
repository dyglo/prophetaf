from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable


CandleRow = dict[str, Any]
SignalRow = dict[str, Any]


@dataclass
class Trade:
    pair: str
    granularity: str
    direction: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    exit_price: float
    risk_amount: float
    position_size: float
    pnl: float
    realized_rr: float
    exit_reason: str
    tp1_hit: bool
    tp2_hit: bool


@dataclass
class BacktestResult:
    trades: list[Trade]
    win_rate: float
    total_pnl: float
    max_drawdown: float
    total_trades: int
    avg_rr: float
    pair: str
    granularity: str
    from_date: str
    to_date: str


@dataclass
class _OpenTrade:
    pair: str
    granularity: str
    direction: str
    signal_time: str
    entry_time: str
    entry_price: float
    stop_loss: float
    active_stop_loss: float
    tp1: float
    tp2: float
    risk_amount: float
    position_size: float
    remaining_size: float
    realized_pnl: float = 0.0
    tp1_hit: bool = False


class BacktestEngine:
    def __init__(
        self,
        candle_loader: Callable[[str, str, str, str], list[CandleRow]],
        signal_detector: Callable[[list[CandleRow]], list[SignalRow]],
    ) -> None:
        self.candle_loader = candle_loader
        self.signal_detector = signal_detector

    def run(
        self,
        pair: str,
        granularity: str,
        from_date: str,
        to_date: str,
        account_size: float = 10000,
        risk_pct: float = 0.01,
    ) -> BacktestResult:
        candles = list(self.candle_loader(pair, granularity, from_date, to_date) or [])
        pending_signals: dict[tuple[Any, ...], SignalRow] = {}
        consumed_signals: set[tuple[Any, ...]] = set()
        open_trade: _OpenTrade | None = None
        trades: list[Trade] = []

        if not candles:
            return BacktestResult(
                trades=[],
                win_rate=0.0,
                total_pnl=0.0,
                max_drawdown=0.0,
                total_trades=0,
                avg_rr=0.0,
                pair=pair,
                granularity=granularity,
                from_date=from_date,
                to_date=to_date,
            )

        risk_amount = account_size * risk_pct
        closed_equity = account_size
        equity_points = [account_size]

        for index in range(1, len(candles)):
            current_candle = candles[index]
            observed_candles = list(candles[:index])
            for signal in self.signal_detector(observed_candles) or []:
                signal_pair = str(signal.get("pair") or pair)
                signal_granularity = str(signal.get("granularity") or granularity)
                if signal_pair != pair or signal_granularity != granularity:
                    continue
                key = self._signal_key(signal)
                if key in consumed_signals:
                    continue
                pending_signals.setdefault(key, signal)

            if open_trade is not None:
                closed_trade = self._advance_open_trade(open_trade, current_candle)
                if closed_trade is not None:
                    trades.append(closed_trade)
                    closed_equity += closed_trade.pnl
                    equity_points.append(closed_equity)
                    open_trade = None
                continue

            for key, signal in list(pending_signals.items()):
                opened_trade = self._maybe_open_trade(
                    signal=signal,
                    current_candle=current_candle,
                    pair=pair,
                    granularity=granularity,
                    risk_amount=risk_amount,
                )
                if opened_trade is None:
                    continue
                pending_signals.pop(key, None)
                consumed_signals.add(key)
                open_trade = opened_trade
                closed_trade = self._advance_open_trade(open_trade, current_candle)
                if closed_trade is not None:
                    trades.append(closed_trade)
                    closed_equity += closed_trade.pnl
                    equity_points.append(closed_equity)
                    open_trade = None
                break

        if open_trade is not None:
            final_candle = candles[-1]
            final_trade = self._close_at_end_of_data(open_trade, final_candle)
            trades.append(final_trade)
            closed_equity += final_trade.pnl
            equity_points.append(closed_equity)

        total_trades = len(trades)
        total_pnl = sum(trade.pnl for trade in trades)
        win_rate = (sum(1 for trade in trades if trade.pnl > 0) / total_trades * 100) if total_trades else 0.0
        avg_rr = (sum(trade.realized_rr for trade in trades) / total_trades) if total_trades else 0.0

        return BacktestResult(
            trades=trades,
            win_rate=win_rate,
            total_pnl=total_pnl,
            max_drawdown=self._max_drawdown(equity_points),
            total_trades=total_trades,
            avg_rr=avg_rr,
            pair=pair,
            granularity=granularity,
            from_date=from_date,
            to_date=to_date,
        )

    def _signal_key(self, signal: SignalRow) -> tuple[Any, ...]:
        return (
            signal.get("type"),
            self._normalize_direction(signal.get("direction")),
            float(signal.get("zone_high", 0.0)),
            float(signal.get("zone_low", 0.0)),
            signal.get("candle_time"),
            signal.get("pair"),
            signal.get("granularity"),
        )

    def _maybe_open_trade(
        self,
        signal: SignalRow,
        current_candle: CandleRow,
        pair: str,
        granularity: str,
        risk_amount: float,
    ) -> _OpenTrade | None:
        zone_low = float(min(signal.get("zone_low", 0.0), signal.get("zone_high", 0.0)))
        zone_high = float(max(signal.get("zone_low", 0.0), signal.get("zone_high", 0.0)))
        candle_low = float(current_candle["low"])
        candle_high = float(current_candle["high"])
        if candle_high < zone_low or candle_low > zone_high:
            return None

        direction = self._normalize_direction(signal.get("direction"))
        if direction not in {"LONG", "SHORT"}:
            return None

        entry_price = zone_high if direction == "LONG" else zone_low
        stop_loss = zone_low if direction == "LONG" else zone_high
        risk_distance = abs(entry_price - stop_loss)
        if risk_distance <= 0:
            return None

        tp1 = entry_price + (risk_distance * 2) if direction == "LONG" else entry_price - (risk_distance * 2)
        tp2 = entry_price + (risk_distance * 3) if direction == "LONG" else entry_price - (risk_distance * 3)
        position_size = risk_amount / risk_distance

        return _OpenTrade(
            pair=pair,
            granularity=granularity,
            direction=direction,
            signal_time=str(signal.get("candle_time") or current_candle["time"]),
            entry_time=str(current_candle["time"]),
            entry_price=entry_price,
            stop_loss=stop_loss,
            active_stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            risk_amount=risk_amount,
            position_size=position_size,
            remaining_size=position_size,
        )

    def _advance_open_trade(self, trade: _OpenTrade, candle: CandleRow) -> Trade | None:
        candle_time = str(candle["time"])
        candle_low = float(candle["low"])
        candle_high = float(candle["high"])

        if not trade.tp1_hit:
            if self._touches_price(candle_low, candle_high, trade.active_stop_loss):
                pnl = self._pnl_for_move(trade.direction, trade.entry_price, trade.active_stop_loss, trade.remaining_size)
                return self._build_trade(trade, candle_time, trade.active_stop_loss, pnl, "stop_loss")

            if self._touches_price(candle_low, candle_high, trade.tp1):
                half_size = trade.position_size * 0.5
                trade.realized_pnl += self._pnl_for_move(trade.direction, trade.entry_price, trade.tp1, half_size)
                trade.remaining_size = max(trade.position_size - half_size, 0.0)
                trade.tp1_hit = True
                trade.active_stop_loss = trade.entry_price

                if self._touches_price(candle_low, candle_high, trade.active_stop_loss):
                    return self._build_trade(trade, candle_time, trade.entry_price, trade.realized_pnl, "breakeven")
                if self._touches_price(candle_low, candle_high, trade.tp2):
                    trade.realized_pnl += self._pnl_for_move(trade.direction, trade.entry_price, trade.tp2, trade.remaining_size)
                    return self._build_trade(trade, candle_time, trade.tp2, trade.realized_pnl, "tp2")
                return None

        else:
            if self._touches_price(candle_low, candle_high, trade.active_stop_loss):
                return self._build_trade(trade, candle_time, trade.entry_price, trade.realized_pnl, "breakeven")
            if self._touches_price(candle_low, candle_high, trade.tp2):
                trade.realized_pnl += self._pnl_for_move(trade.direction, trade.entry_price, trade.tp2, trade.remaining_size)
                return self._build_trade(trade, candle_time, trade.tp2, trade.realized_pnl, "tp2")

        return None

    def _close_at_end_of_data(self, trade: _OpenTrade, candle: CandleRow) -> Trade:
        exit_price = float(candle["close"])
        pnl = trade.realized_pnl + self._pnl_for_move(trade.direction, trade.entry_price, exit_price, trade.remaining_size)
        return self._build_trade(trade, str(candle["time"]), exit_price, pnl, "end_of_data")

    def _build_trade(
        self,
        trade: _OpenTrade,
        exit_time: str,
        exit_price: float,
        pnl: float,
        exit_reason: str,
    ) -> Trade:
        return Trade(
            pair=trade.pair,
            granularity=trade.granularity,
            direction=trade.direction,
            signal_time=trade.signal_time,
            entry_time=trade.entry_time,
            exit_time=exit_time,
            entry_price=trade.entry_price,
            stop_loss=trade.stop_loss,
            tp1=trade.tp1,
            tp2=trade.tp2,
            exit_price=exit_price,
            risk_amount=trade.risk_amount,
            position_size=trade.position_size,
            pnl=pnl,
            realized_rr=(pnl / trade.risk_amount) if trade.risk_amount else 0.0,
            exit_reason=exit_reason,
            tp1_hit=trade.tp1_hit,
            tp2_hit=exit_reason == "tp2",
        )

    def _normalize_direction(self, direction: Any) -> str:
        value = str(direction or "").strip().upper()
        aliases = {
            "BUY": "LONG",
            "BULLISH": "LONG",
            "LONG": "LONG",
            "SELL": "SHORT",
            "BEARISH": "SHORT",
            "SHORT": "SHORT",
        }
        return aliases.get(value, value)

    def _touches_price(self, candle_low: float, candle_high: float, price: float) -> bool:
        return candle_low <= price <= candle_high

    def _pnl_for_move(self, direction: str, entry_price: float, exit_price: float, size: float) -> float:
        if direction == "LONG":
            return (exit_price - entry_price) * size
        return (entry_price - exit_price) * size

    def _max_drawdown(self, equity_points: list[float]) -> float:
        peak = equity_points[0] if equity_points else 0.0
        max_drawdown = 0.0
        for equity in equity_points:
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        return max_drawdown


def serialize_backtest_result(result: BacktestResult) -> dict[str, Any]:
    return {
        "trades": [asdict(trade) for trade in result.trades],
        "win_rate": result.win_rate,
        "total_pnl": result.total_pnl,
        "max_drawdown": result.max_drawdown,
        "total_trades": result.total_trades,
        "avg_rr": result.avg_rr,
        "pair": result.pair,
        "granularity": result.granularity,
        "from_date": result.from_date,
        "to_date": result.to_date,
    }
