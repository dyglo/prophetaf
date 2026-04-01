from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import httpx

from hedge_fund.config.environment import EnvironmentSettings


logger = logging.getLogger(__name__)

OANDA_BASE_URL = "https://api-fxpractice.oanda.com/v3"
MAX_CANDLES_PER_REQUEST = 5000
MAX_RETRIES = 3
GRANULARITY_STEPS = {
    "H1": timedelta(hours=1),
    "M15": timedelta(minutes=15),
}


class CandleRow(TypedDict):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    pair: str
    granularity: str


def fetch_candles(
    instrument: str,
    granularity: str,
    from_date: datetime | str,
    to_date: datetime | str,
) -> list[CandleRow]:
    step = GRANULARITY_STEPS.get(granularity)
    if step is None:
        raise ValueError(f"Unsupported granularity: {granularity}")

    start = _coerce_datetime(from_date)
    end = _coerce_datetime(to_date)
    if start >= end:
        return []

    batch_rows: list[CandleRow] = []
    current_from = start
    include_first = True
    batch_number = 0
    timeout = httpx.Timeout(30.0)

    while current_from < end:
        current_to = min(current_from + (step * MAX_CANDLES_PER_REQUEST), end)
        payload = _request_candles_batch(
            instrument=instrument,
            granularity=granularity,
            start=current_from,
            end=current_to,
            include_first=include_first,
            timeout=timeout,
        )
        candles = payload.get("candles", [])
        fetched_rows = [
            _to_candle_row(item=item, instrument=instrument, granularity=granularity)
            for item in candles
            if item.get("complete", True)
        ]
        batch_number += 1
        logger.info(
            "Fetched candle batch %s for %s %s: %s candles between %s and %s",
            batch_number,
            instrument,
            granularity,
            len(fetched_rows),
            _to_iso_z(current_from),
            _to_iso_z(current_to),
        )
        batch_rows.extend(fetched_rows)

        if fetched_rows:
            current_from = _coerce_datetime(fetched_rows[-1]["time"])
            if current_from + step >= end:
                break
            include_first = False
            continue

        current_from = current_to
        include_first = True

    return batch_rows


def _request_candles_batch(
    *,
    instrument: str,
    granularity: str,
    start: datetime,
    end: datetime,
    include_first: bool,
    timeout: httpx.Timeout,
) -> dict:
    env = EnvironmentSettings.load()
    if not env.oanda_api_key:
        raise ValueError("Missing OANDA_API_KEY")

    url = f"{OANDA_BASE_URL}/instruments/{instrument}/candles"
    headers = {"Authorization": f"Bearer {env.oanda_api_key}"}
    params = {
        "price": "M",
        "granularity": granularity,
        "from": _to_iso_z(start),
        "to": _to_iso_z(end),
        "includeFirst": include_first,
    }

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = httpx.get(url, headers=headers, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if _is_retryable_status(status_code) and attempt < MAX_RETRIES:
                _sleep_before_retry(attempt, instrument, granularity, status_code)
                continue
            raise
        except httpx.RequestError:
            if attempt < MAX_RETRIES:
                _sleep_before_retry(attempt, instrument, granularity, "request_error")
                continue
            raise

    raise RuntimeError("Unreachable retry loop state")


def _sleep_before_retry(attempt: int, instrument: str, granularity: str, reason: int | str) -> None:
    delay_seconds = 2**attempt
    logger.warning(
        "Retrying OANDA candle fetch for %s %s after %s seconds due to %s",
        instrument,
        granularity,
        delay_seconds,
        reason,
    )
    time.sleep(delay_seconds)


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _to_candle_row(*, item: dict, instrument: str, granularity: str) -> CandleRow:
    mid = item["mid"]
    return {
        "time": _to_iso_z(_coerce_datetime(item["time"])),
        "open": float(mid["o"]),
        "high": float(mid["h"]),
        "low": float(mid["l"]),
        "close": float(mid["c"]),
        "volume": int(item.get("volume", 0)),
        "pair": instrument,
        "granularity": granularity,
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
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
