from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    select,
)

from hedge_fund.config.environment import EnvironmentSettings
from hedge_fund.storage.session import build_session_factory
from tools.historical_data import CandleRow


metadata = MetaData()
historical_candles = Table(
    "historical_candles",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("time", DateTime(timezone=True), nullable=False),
    Column("pair", String(20), nullable=False),
    Column("granularity", String(10), nullable=False),
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("volume", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "time",
        "pair",
        "granularity",
        name="uq_historical_candles_time_pair_granularity",
    ),
    Index(
        "ix_historical_candles_pair_granularity_time",
        "pair",
        "granularity",
        "time",
    ),
)

_SESSION_FACTORY = None


def store_candles(candles: list[CandleRow]) -> int:
    if not candles:
        return 0

    rows = [_normalize_candle_row(candle) for candle in candles]
    session = _get_session_factory()()
    try:
        statement = _build_insert_statement(session.get_bind().dialect.name, rows)
        result = session.execute(statement)
        session.commit()
        return max(result.rowcount or 0, 0)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def load_candles(
    pair: str,
    granularity: str,
    from_date: datetime | str,
    to_date: datetime | str,
) -> list[CandleRow]:
    start = _coerce_datetime(from_date)
    end = _coerce_datetime(to_date)
    session = _get_session_factory()()
    try:
        rows = session.execute(
            select(
                historical_candles.c.time,
                historical_candles.c.open,
                historical_candles.c.high,
                historical_candles.c.low,
                historical_candles.c.close,
                historical_candles.c.volume,
                historical_candles.c.pair,
                historical_candles.c.granularity,
            )
            .where(historical_candles.c.pair == pair)
            .where(historical_candles.c.granularity == granularity)
            .where(historical_candles.c.time >= start)
            .where(historical_candles.c.time <= end)
            .order_by(historical_candles.c.time.asc())
        ).all()
        return [
            {
                "time": _to_iso_z(_coerce_datetime(row.time)),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": int(row.volume),
                "pair": str(row.pair),
                "granularity": str(row.granularity),
            }
            for row in rows
        ]
    finally:
        session.close()


def cache_exists(
    pair: str,
    granularity: str,
    from_date: datetime | str,
    to_date: datetime | str,
) -> bool:
    start = _coerce_datetime(from_date)
    end = _coerce_datetime(to_date)
    session = _get_session_factory()()
    try:
        minimum, maximum = session.execute(
            select(
                func.min(historical_candles.c.time),
                func.max(historical_candles.c.time),
            )
            .where(historical_candles.c.pair == pair)
            .where(historical_candles.c.granularity == granularity)
        ).one()
        if minimum is None or maximum is None:
            return False
        return _coerce_datetime(minimum) <= start and _coerce_datetime(maximum) >= end
    finally:
        session.close()


def _get_session_factory():
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        env = EnvironmentSettings.load()
        _SESSION_FACTORY = build_session_factory(env.database_url)
    return _SESSION_FACTORY


def _build_insert_statement(dialect_name: str, rows: list[dict]):
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as insert_builder
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as insert_builder
    else:
        raise ValueError(f"Unsupported SQL dialect: {dialect_name}")

    return insert_builder(historical_candles).values(rows).on_conflict_do_nothing(
        index_elements=["time", "pair", "granularity"]
    )


def _normalize_candle_row(candle: CandleRow) -> dict:
    return {
        "time": _coerce_datetime(candle["time"]),
        "pair": candle["pair"],
        "granularity": candle["granularity"],
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "volume": int(candle["volume"]),
        "created_at": datetime.now(tz=UTC),
    }


def _coerce_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_iso_z(value: datetime) -> str:
    return _coerce_datetime(value).isoformat().replace("+00:00", "Z")
