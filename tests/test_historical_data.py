from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tools.data_cache as data_cache
from tools.data_cache import historical_candles
from tools.historical_data import fetch_candles


def _response(status_code: int, payload: dict) -> httpx.Response:
    request = httpx.Request("GET", "https://api-fxpractice.oanda.com/v3/instruments/EUR_USD/candles")
    return httpx.Response(status_code=status_code, json=payload, request=request)


def _candle_payload(timestamp: datetime, *, complete: bool = True, volume: int = 10) -> dict:
    return {
        "time": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "complete": complete,
        "volume": volume,
        "mid": {
            "o": "1.1000",
            "h": "1.2000",
            "l": "1.0500",
            "c": "1.1500",
        },
    }


@pytest.fixture(autouse=True)
def _clear_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_cache, "_SESSION_FACTORY", None)


def test_fetch_candles_returns_normalized_rows_and_skips_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_get(url, headers, params, timeout):
        calls.append({"url": url, "headers": headers, "params": params})
        return _response(
            200,
            {
                "candles": [
                    _candle_payload(datetime(2026, 3, 1, tzinfo=UTC)),
                    _candle_payload(datetime(2026, 3, 1, 1, tzinfo=UTC), complete=False),
                ]
            },
        )

    monkeypatch.setattr("tools.historical_data.httpx.get", fake_get)
    monkeypatch.setattr(
        "tools.historical_data.EnvironmentSettings.load",
        lambda: type("Env", (), {"oanda_api_key": "token"})(),
    )

    candles = fetch_candles("EUR_USD", "H1", "2026-03-01T00:00:00Z", "2026-03-01T01:00:00Z")

    assert len(candles) == 1
    assert candles[0] == {
        "time": "2026-03-01T00:00:00Z",
        "open": 1.1,
        "high": 1.2,
        "low": 1.05,
        "close": 1.15,
        "volume": 10,
        "pair": "EUR_USD",
        "granularity": "H1",
    }
    assert calls[0]["params"]["includeFirst"] is True


def test_fetch_candles_paginates_without_duplicate_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    request_params: list[dict] = []
    first_timestamp = datetime(2026, 3, 1, tzinfo=UTC)
    boundary_timestamp = first_timestamp + timedelta(hours=5000)
    last_timestamp = boundary_timestamp + timedelta(hours=1)

    responses = [
        _response(
            200,
            {
                "candles": [
                    _candle_payload(first_timestamp),
                    _candle_payload(boundary_timestamp),
                ]
            },
        ),
        _response(
            200,
            {
                "candles": [
                    _candle_payload(last_timestamp),
                ]
            },
        ),
    ]

    def fake_get(url, headers, params, timeout):
        request_params.append(params.copy())
        return responses.pop(0)

    monkeypatch.setattr("tools.historical_data.httpx.get", fake_get)
    monkeypatch.setattr(
        "tools.historical_data.EnvironmentSettings.load",
        lambda: type("Env", (), {"oanda_api_key": "token"})(),
    )

    candles = fetch_candles(
        "EUR_USD",
        "H1",
        first_timestamp,
        last_timestamp + timedelta(hours=1),
    )

    assert len(request_params) == 2
    assert request_params[0]["includeFirst"] is True
    assert request_params[1]["includeFirst"] is False
    assert request_params[1]["from"] == "2026-09-25T08:00:00Z"
    assert [candle["time"] for candle in candles] == [
        "2026-03-01T00:00:00Z",
        "2026-09-25T08:00:00Z",
        "2026-09-25T09:00:00Z",
    ]


def test_fetch_candles_retries_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[int] = []
    responses = [
        _response(429, {"candles": []}),
        _response(429, {"candles": []}),
        _response(200, {"candles": [_candle_payload(datetime(2026, 3, 1, tzinfo=UTC))]}),
    ]

    monkeypatch.setattr("tools.historical_data.httpx.get", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr("tools.historical_data.time.sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(
        "tools.historical_data.EnvironmentSettings.load",
        lambda: type("Env", (), {"oanda_api_key": "token"})(),
    )

    candles = fetch_candles("EUR_USD", "H1", "2026-03-01T00:00:00Z", "2026-03-01T01:00:00Z")

    assert len(candles) == 1
    assert sleep_calls == [1, 2]


def test_fetch_candles_raises_after_exhausting_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[int] = []

    monkeypatch.setattr(
        "tools.historical_data.httpx.get",
        lambda *args, **kwargs: _response(429, {"candles": []}),
    )
    monkeypatch.setattr("tools.historical_data.time.sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(
        "tools.historical_data.EnvironmentSettings.load",
        lambda: type("Env", (), {"oanda_api_key": "token"})(),
    )

    with pytest.raises(httpx.HTTPStatusError):
        fetch_candles("EUR_USD", "H1", "2026-03-01T00:00:00Z", "2026-03-01T01:00:00Z")

    assert sleep_calls == [1, 2, 4]


@pytest.fixture
def cache_session_factory(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    data_cache.metadata.create_all(engine)
    monkeypatch.setattr(data_cache, "_SESSION_FACTORY", SessionLocal)
    return SessionLocal


def test_store_and_load_candles_round_trip(cache_session_factory) -> None:
    inserted = data_cache.store_candles(
        [
            {
                "time": "2026-03-01T00:00:00Z",
                "open": 1.1,
                "high": 1.2,
                "low": 1.05,
                "close": 1.15,
                "volume": 10,
                "pair": "EUR_USD",
                "granularity": "H1",
            },
            {
                "time": "2026-03-01T01:00:00Z",
                "open": 1.15,
                "high": 1.25,
                "low": 1.1,
                "close": 1.2,
                "volume": 12,
                "pair": "EUR_USD",
                "granularity": "H1",
            },
        ]
    )

    loaded = data_cache.load_candles("EUR_USD", "H1", "2026-03-01T00:00:00Z", "2026-03-01T01:00:00Z")

    assert inserted == 2
    assert [row["time"] for row in loaded] == [
        "2026-03-01T00:00:00Z",
        "2026-03-01T01:00:00Z",
    ]
    assert loaded[0]["pair"] == "EUR_USD"
    assert loaded[0]["granularity"] == "H1"


def test_store_candles_deduplicates_upserts(cache_session_factory) -> None:
    candle = {
        "time": "2026-03-01T00:00:00Z",
        "open": 1.1,
        "high": 1.2,
        "low": 1.05,
        "close": 1.15,
        "volume": 10,
        "pair": "EUR_USD",
        "granularity": "H1",
    }

    first_insert = data_cache.store_candles([candle])
    second_insert = data_cache.store_candles([candle])

    with cache_session_factory() as session:
        total_rows = session.execute(select(func.count()).select_from(historical_candles)).scalar_one()

    assert first_insert == 1
    assert second_insert == 0
    assert total_rows == 1


def test_cache_exists_detects_present_and_absent_ranges(cache_session_factory) -> None:
    data_cache.store_candles(
        [
            {
                "time": "2026-03-01T00:00:00Z",
                "open": 1.1,
                "high": 1.2,
                "low": 1.05,
                "close": 1.15,
                "volume": 10,
                "pair": "EUR_USD",
                "granularity": "H1",
            },
            {
                "time": "2026-03-01T05:00:00Z",
                "open": 1.15,
                "high": 1.25,
                "low": 1.1,
                "close": 1.2,
                "volume": 12,
                "pair": "EUR_USD",
                "granularity": "H1",
            },
        ]
    )

    assert data_cache.cache_exists("EUR_USD", "H1", "2026-03-01T00:00:00Z", "2026-03-01T05:00:00Z") is True
    assert data_cache.cache_exists("EUR_USD", "H1", "2026-03-01T00:00:00Z", "2026-03-01T06:00:00Z") is False
    assert data_cache.cache_exists("GBP_USD", "H1", "2026-03-01T00:00:00Z", "2026-03-01T05:00:00Z") is False
