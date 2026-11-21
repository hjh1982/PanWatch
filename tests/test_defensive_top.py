"""高股息防御三巨头做头观察：回撤/均线计算、窗口、数据不足与路由测试。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.market import defensive_top_service
from src.modules.market.api import market as market_api
from src.platform.marketdata.collectors.kline_collector import KlineData


def _bars(prices: list[float]) -> list[KlineData]:
    start = date(2026, 5, 1)
    return [
        KlineData(
            date=(start + timedelta(days=i)).isoformat(),
            open=price,
            close=price,
            high=price * 1.01,
            low=price * 0.99,
            volume=1000,
        )
        for i, price in enumerate(prices)
    ]


def _declining_prices() -> list[float]:
    """先涨到 105.9，再用 40 个交易日跌到 90：回撤约 15%，均线拐头向下。"""
    rising = [100.0 + i * 0.1 for i in range(60)]
    falling = [105.9 - (i - 59) * (15.9 / 40) for i in range(60, 100)]
    return rising + falling


def test_计算回撤均线位置与方向():
    bars = _bars(_declining_prices())

    stock = defensive_top_service.analyze_stock(defensive_top_service.LEADERS[0], bars)

    assert stock["available"] is True
    assert stock["high60"] == pytest.approx(105.9)
    assert stock["high60_date"] == bars[59].date
    assert stock["close"] == pytest.approx(90.0)
    assert stock["drawdown_pct"] == pytest.approx(-15.01, abs=0.02)
    assert stock["vs_ma20_pct"] < 0
    assert stock["vs_ma60_pct"] < 0
    assert stock["ma20_slope_pct"] < 0
    assert stock["ma60_slope_pct"] < 0
    assert len(stock["spark"]) == 60
    assert stock["spark"][-1] == pytest.approx(90.0)


def test_最高收盘只看最近60个交易日():
    # 窗口外的 200 不能影响最近 60 日高点（窗口内最高是最后一天的 198）
    prices = [200.0] + [100.0 + i for i in range(99)]

    stock = defensive_top_service.analyze_stock(defensive_top_service.LEADERS[0], _bars(prices))

    assert stock["high60"] == pytest.approx(198.0)
    assert stock["drawdown_pct"] == pytest.approx(0.0)


def test_数据不足时标记不可用():
    stock = defensive_top_service.analyze_stock(
        defensive_top_service.LEADERS[1], _bars([100.0] * 10)
    )

    assert stock == {
        "symbol": "600900",
        "name": "长江电力",
        "available": False,
        "reason": "insufficient",
    }


def test_两只同时回撤且跌破20日线时给出集体预警():
    declining = _declining_prices()
    flat = [100.0] * 100
    klines = {
        "601088": _bars(declining),
        "600900": _bars(flat),
        "601398": _bars(declining),
    }

    payload = defensive_top_service.build_payload(klines)

    assert payload["collective"]["triggered"] == 2
    assert payload["collective"]["warning"] is True
    assert payload["collective"]["symbols"] == ["601088", "601398"]
    assert payload["as_of"] == _bars(declining)[-1].date


def test_仅一只回撤时不触发集体预警():
    declining = _declining_prices()
    flat = [100.0] * 100
    klines = {
        "601088": _bars(declining),
        "600900": _bars(flat),
        "601398": _bars(flat),
    }

    payload = defensive_top_service.build_payload(klines)

    assert payload["collective"]["triggered"] == 1
    assert payload["collective"]["warning"] is False


def test_K线获取失败时返回不可用而不抛错(monkeypatch):
    def boom(symbol: str):
        raise RuntimeError("offline")

    monkeypatch.setattr(defensive_top_service, "load_klines", boom)

    payload = defensive_top_service.get_payload()

    assert [stock["available"] for stock in payload["stocks"]] == [False, False, False]
    assert payload["collective"]["warning"] is False
    assert payload["as_of"] is None


def test_路由返回三巨头状态(monkeypatch):
    payload = {
        "as_of": "2026-11-20",
        "stocks": [],
        "collective": {"warning": False, "triggered": 0, "total": 3},
    }
    monkeypatch.setattr(defensive_top_service, "get_payload", lambda: payload)

    app = FastAPI()
    app.include_router(market_api.router, prefix="/api/market")

    response = TestClient(app).get("/api/market/defensive-top")

    assert response.status_code == 200
    assert response.json() == payload
