"""高股息防御三巨头做头观察：中国神华 / 长江电力 / 工商银行。

只做状态展示，不判定个股：60 日最高收盘回撤、现价与 20/60 日线位置、均线方向。
集体预警口径固定为「≥2 只同时回撤 ≥8% 且跌破 20 日线」，随返回值一起下发，
供首页原样标注，避免前端再维护一套阈值。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from src.platform.marketdata.collectors.kline_collector import KlineCollector, KlineData
from src.platform.marketdata.models import MarketCode

logger = logging.getLogger(__name__)

WINDOW_DAYS = 60
DRAWDOWN_ALERT_PCT = 8.0
COLLECTIVE_TRIGGER_COUNT = 2
_KLINE_DAYS = 120


@dataclass(frozen=True, slots=True)
class LeaderDef:
    symbol: str
    name: str


LEADERS: tuple[LeaderDef, ...] = (
    LeaderDef("601088", "中国神华"),
    LeaderDef("600900", "长江电力"),
    LeaderDef("601398", "工商银行"),
)


def _pct(value: float, base: float | None) -> float | None:
    if base is None or base <= 0:
        return None
    return round((value / base - 1.0) * 100.0, 2)


def _ma(closes: list[float], period: int, offset: int = 0) -> float | None:
    end = len(closes) - offset
    start = end - period
    if start < 0:
        return None
    return sum(closes[start:end]) / period


def analyze_stock(definition: LeaderDef, klines: list[KlineData]) -> dict:
    base = {"symbol": definition.symbol, "name": definition.name}
    bars = [bar for bar in klines if bar.close > 0]
    if len(bars) < 20:
        return {**base, "available": False, "reason": "insufficient"}

    closes = [bar.close for bar in bars]
    latest = bars[-1]
    window = bars[-WINDOW_DAYS:]
    high_bar = max(window, key=lambda bar: bar.close)

    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    ma20_prev = _ma(closes, 20, offset=5)
    ma60_prev = _ma(closes, 60, offset=5)

    return {
        **base,
        "available": True,
        "date": latest.date,
        "close": round(latest.close, 3),
        "change_pct": _pct(latest.close, bars[-2].close) if len(bars) >= 2 else None,
        "high60": round(high_bar.close, 3),
        "high60_date": high_bar.date,
        "drawdown_pct": _pct(latest.close, high_bar.close),
        "ma20": round(ma20, 3) if ma20 is not None else None,
        "ma60": round(ma60, 3) if ma60 is not None else None,
        "vs_ma20_pct": _pct(latest.close, ma20),
        "vs_ma60_pct": _pct(latest.close, ma60),
        "ma20_slope_pct": _pct(ma20, ma20_prev) if ma20 is not None else None,
        "ma60_slope_pct": _pct(ma60, ma60_prev) if ma60 is not None else None,
        "spark": [round(close, 3) for close in closes[-WINDOW_DAYS:]],
    }


def _topping_like(stock: dict) -> bool:
    drawdown = stock.get("drawdown_pct")
    vs_ma20 = stock.get("vs_ma20_pct")
    return (
        bool(stock.get("available"))
        and drawdown is not None
        and drawdown <= -DRAWDOWN_ALERT_PCT
        and vs_ma20 is not None
        and vs_ma20 < 0
    )


def build_payload(klines_by_symbol: dict[str, list[KlineData]]) -> dict:
    stocks = [
        analyze_stock(definition, klines_by_symbol.get(definition.symbol) or [])
        for definition in LEADERS
    ]
    triggered = [stock["symbol"] for stock in stocks if _topping_like(stock)]
    dates = [stock["date"] for stock in stocks if stock.get("date")]
    return {
        "as_of": max(dates) if dates else None,
        "stocks": stocks,
        "collective": {
            "warning": len(triggered) >= COLLECTIVE_TRIGGER_COUNT,
            "triggered": len(triggered),
            "total": len(LEADERS),
            "threshold": COLLECTIVE_TRIGGER_COUNT,
            "drawdown_pct": DRAWDOWN_ALERT_PCT,
            "symbols": triggered,
        },
    }


def load_klines(symbol: str) -> list[KlineData]:
    return KlineCollector(MarketCode.CN).get_klines(symbol, days=_KLINE_DAYS)


def get_payload() -> dict:
    with ThreadPoolExecutor(max_workers=len(LEADERS)) as pool:
        futures = {
            definition.symbol: pool.submit(load_klines, definition.symbol)
            for definition in LEADERS
        }
        klines_by_symbol: dict[str, list[KlineData]] = {}
        for symbol, future in futures.items():
            try:
                klines_by_symbol[symbol] = future.result()
            except Exception as exc:
                logger.warning("防御龙头K线获取失败 %s: %s", symbol, exc)
                klines_by_symbol[symbol] = []
    return build_payload(klines_by_symbol)
