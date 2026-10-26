"""全球主要煤炭价格：ICE 纽卡斯尔/鹿特丹动力煤与国内焦煤期货。

三个数据源行情口径不同（ICE 为美元/吨，大商所为元/吨），前端用双轴展示；
纽卡斯尔在东方财富仅有 2026-08-25 起的连续合约日线，需在 UI 明确标注。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from src.modules.market.etf_share_service import _require_group
from src.platform.marketdata.collectors.market_http import TTLCache, market_get

logger = logging.getLogger(__name__)

GLOBAL_COAL_GROUP_ID = "globalCoal"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_EASTMONEY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_EASTMONEY_PARAMS = {
    "secid": "112.NCF00Y",
    "klt": "101",
    "fqt": "1",
    "lmt": "1000000",
    "end": "20500000",
    "iscca": "1",
    "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    "ut": "7eea3edcaed734bea9cbfc24409ed989",
    "forcect": "1",
}
_CNBC_URL = "https://ts-api.cnbc.com/harmony/app/charts/1Y.json"
_SINA_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/json.php/"
    "InnerFuturesNewService.getDailyKLine"
)
_CACHE_TTL_S = 600


@dataclass(frozen=True, slots=True)
class SeriesDef:
    id: str
    label: str
    axis: str
    unit: str
    provider: str
    url: str
    cadence: str


SERIES: tuple[SeriesDef, ...] = (
    SeriesDef(
        id="newcastle",
        label="ICE-Newcastle 煤",
        axis="left",
        unit="美元/吨",
        provider="东方财富 · ICE 纽卡斯尔动力煤连续",
        url="https://quote.eastmoney.com/globalfuture/NCF00Y.html",
        cadence="日线",
    ),
    SeriesDef(
        id="rotterdam",
        label="ICE-Rotterdam 煤",
        axis="left",
        unit="美元/吨",
        provider="CNBC · ICE API2 鹿特丹动力煤连续",
        url="https://www.cnbc.com/quotes/@ATW.1",
        cadence="日线",
    ),
    SeriesDef(
        id="coking_coal",
        label="中国-焦煤期货",
        axis="right",
        unit="元/吨",
        provider="新浪财经 · 大商所焦煤主连",
        url="https://finance.sina.com.cn/futures/quotes/JM0.shtml",
        cadence="日线",
    ),
)
_cache = TTLCache(default_ttl_sec=_CACHE_TTL_S)
_locks = {item.id: threading.Lock() for item in SERIES}


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_eastmoney_payload(payload: Any) -> dict[str, float]:
    """东财 K 线：data.klines 每行逗号分隔，第 1 列为日期、第 3 列为收盘价。"""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    klines = data.get("klines")
    if not isinstance(klines, list):
        return {}
    rows: dict[str, float] = {}
    for item in klines:
        if not isinstance(item, str):
            continue
        parts = item.split(",")
        if len(parts) < 3:
            continue
        day = parts[0].strip()
        close = _to_float(parts[2])
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or close is None or close <= 0:
            continue
        rows[day] = close
    return dict(sorted(rows.items()))


def parse_cnbc_payload(payload: Any) -> dict[str, float]:
    """CNBC 图表接口：barData.priceBars[].tradeTime(YYYYMMDD) + close。"""
    if not isinstance(payload, dict):
        return {}
    bar_data = payload.get("barData")
    if not isinstance(bar_data, dict):
        return {}
    bars = bar_data.get("priceBars")
    if not isinstance(bars, list):
        return {}
    rows: dict[str, float] = {}
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        stamp = str(bar.get("tradeTime") or "")
        if len(stamp) < 8 or not stamp[:8].isdigit():
            continue
        close = _to_float(bar.get("close"))
        if close is None or close <= 0:
            continue
        rows[f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"] = close
    return dict(sorted(rows.items()))


def _extract_sina_items(text: Any) -> list[Any]:
    if isinstance(text, list):
        return text
    if isinstance(text, dict):
        result = text.get("result")
        if isinstance(result, dict) and isinstance(result.get("data"), list):
            return result["data"]
        return []
    if not isinstance(text, str):
        return []
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(text[start : end + 1])
    except ValueError:
        return []
    return items if isinstance(items, list) else []


def parse_sina_payload(text: Any) -> dict[str, float]:
    """新浪日线：数组元素取 d(日期) 与 c(收盘价)，兼容 JSON 与 JSONP 包装。"""
    if isinstance(text, str):
        try:
            text = json.loads(text)
        except ValueError:
            pass
    rows: dict[str, float] = {}
    for item in _extract_sina_items(text):
        if not isinstance(item, dict):
            continue
        day = str(item.get("d") or "").strip()
        close = _to_float(item.get("c"))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or close is None or close <= 0:
            continue
        rows[day] = close
    return dict(sorted(rows.items()))


def _fetch_eastmoney() -> dict[str, float]:
    payload = market_get(
        _EASTMONEY_URL,
        host_key="push2his.eastmoney.com",
        params=_EASTMONEY_PARAMS,
        headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"},
        min_interval_s=1.0,
        parse="json",
        force_ipv4=True,
        symbol="NCF00Y",
        log_label="纽卡斯尔煤炭",
    )
    return parse_eastmoney_payload(payload)


def _fetch_cnbc() -> dict[str, float]:
    payload = market_get(
        _CNBC_URL,
        host_key="ts-api.cnbc.com",
        params={"symbol": "@ATW.1", "interval": "1D"},
        headers={"User-Agent": _UA, "Referer": "https://www.cnbc.com/"},
        parse="json",
        symbol="ATW.1",
        log_label="鹿特丹煤炭",
    )
    return parse_cnbc_payload(payload)


def _fetch_sina() -> dict[str, float]:
    payload = market_get(
        _SINA_URL,
        host_key="stock2.finance.sina.com.cn",
        params={"symbol": "JM0", "_": int(time.time() * 1000)},
        headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"},
        parse="json",
        symbol="JM0",
        log_label="焦煤期货",
    )
    return parse_sina_payload(payload)


_FETCHERS = {
    "newcastle": _fetch_eastmoney,
    "rotterdam": _fetch_cnbc,
    "coking_coal": _fetch_sina,
}


def _load_series(series_id: str, *, force: bool) -> dict[str, float]:
    if not force:
        cached = _cache.get(series_id)
        if isinstance(cached, dict):
            return cached
    with _locks[series_id]:
        if not force:
            cached = _cache.get(series_id)
            if isinstance(cached, dict):
                return cached
        rows: dict[str, float] = {}
        for attempt in range(3):
            rows = _FETCHERS[series_id]()
            if rows:
                break
            if attempt < 2:
                time.sleep(1.0 + attempt)
        if rows:
            _cache.set(series_id, rows)
        return rows


def load_rows(*, force: bool = False) -> dict[str, dict[str, float]]:
    with ThreadPoolExecutor(max_workers=len(SERIES)) as pool:
        futures = {
            item.id: pool.submit(_load_series, item.id, force=force)
            for item in SERIES
        }
        return {series_id: future.result() for series_id, future in futures.items()}


def _metric(
    definition: SeriesDef,
    dates: list[str],
    values: list[float | None],
) -> dict[str, Any] | None:
    present = [
        (day, value)
        for day, value in zip(dates, values)
        if isinstance(value, (int, float))
    ]
    if not present:
        return None
    latest_date, latest = present[-1]
    previous = present[-2][1] if len(present) > 1 else None
    delta = round(latest - previous, 4) if previous is not None else None
    delta_pct = (
        round((latest - previous) / previous * 100, 4)
        if previous not in (None, 0)
        else None
    )
    return {
        "id": definition.id,
        "label": definition.label,
        "unit": definition.unit,
        "axis": definition.axis,
        "latest": round(float(latest), 4),
        "latest_date": latest_date,
        "delta": delta,
        "delta_pct": delta_pct,
    }


def build_payload(
    rows: dict[str, dict[str, float]],
    days: int,
) -> dict[str, Any]:
    group = _require_group(GLOBAL_COAL_GROUP_ID)
    union = sorted({day for series in rows.values() for day in series})
    dates = union[-max(days, 1) :] if union else []

    series: dict[str, list[float | None]] = {}
    for definition in SERIES:
        points = rows.get(definition.id, {})
        series[definition.id] = [points.get(day) for day in dates]

    metrics = [
        metric
        for metric in (_metric(definition, dates, series[definition.id]) for definition in SERIES)
        if metric is not None
    ]
    as_of = max(
        (metric["latest_date"] for metric in metrics),
        default=None,
    )
    covered = sum(
        1 for index in range(len(dates)) if any(series[key][index] is not None for key in series)
    )
    complete = bool(dates) and all(
        values[-1] is not None for values in series.values()
    )

    sources = []
    for definition in SERIES:
        points = rows.get(definition.id, {})
        sources.append(
            {
                "id": definition.id,
                "name": definition.provider,
                "url": definition.url,
                "cadence": definition.cadence,
                "start": next(iter(points), None),
                "latest": next(reversed(points), None) if points else None,
            }
        )

    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "ICE 纽卡斯尔/鹿特丹动力煤 · 大商所焦煤主连",
        "as_of": as_of,
        "dates": dates,
        "metrics": metrics,
        "series": series,
        "axes": {
            "left": {"label": "ICE 动力煤", "unit": "美元/吨"},
            "right": {"label": "焦煤期货", "unit": "元/吨"},
        },
        "sources": sources,
        "coverage": {
            "trading_days": covered,
            "target": len(dates),
            "complete": complete,
        },
    }


def get_curve_payload(
    group_id: str,
    days: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    _require_group(group_id)
    return build_payload(load_rows(force=force), days)
