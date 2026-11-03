"""A 股股息率：申万风格指数日度股息率与 10 年期国债收益率。

申万宏源研究的指数分析接口提供申万 A 指、大盘/中盘/小盘风格指数的日度
估值，其中 ``dp`` 字段即整体法股息率（百分数）。中国 10 年期国债收益率
取自东方财富的中美国债收益率序列，作为无风险利率对比。两者按交易日对齐，
国债收益率缺失日沿用最近一次公布值；利差 = 全 A 股息率 - 10 年期国债收益率。

口径说明：
- 股息率为申万口径（整体法，按指数成分股总股本计算），与中证指数官网的
  「总股本/计算用股本」两种口径可能存在差异。
- 申万日度估值覆盖全部 A 股与风格指数；本页只取近 5 年，避免早期口径变化。
- 国债收益率为银行间市场收盘值，单位 %；利差单位为百分点（pp）。
"""

from __future__ import annotations

import logging
import threading
from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.modules.market.etf_share_service import _require_group
from src.platform.marketdata.collectors.market_http import TTLCache, market_get

logger = logging.getLogger(__name__)

DIVIDEND_YIELD_GROUP_ID = "dividendYield"

_CN_TZ = ZoneInfo("Asia/Shanghai")
_CACHE_TTL_S = 6 * 3600
_LOOKBACK_DAYS = 365 * 5 + 7
_PRIOR_DAYS = 30
_MAX_BOND_PAGES = 8

_SW_URL = "https://www.swsresearch.com/institute-sw/api/index_analysis/index_analysis_report/"
_SW_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.swsresearch.com/institute_sw/allIndex/analysisIndex",
}
_SW_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("all", "801003", "市场表征"),
    ("large", "801811", "风格指数"),
    ("mid", "801812", "风格指数"),
    ("small", "801813", "风格指数"),
)

_BOND_URL = "https://datacenter.eastmoney.com/api/data/get"
_BOND_TOKEN = "894050c76af8597a853f5b408b759f5d"
_BOND_10Y_FIELD = "EMM00166466"
_BOND_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/cjsj/zmgzsyl.html",
}

_SERIES_LABELS = {
    "all": "全A股息率",
    "large": "大盘股息率",
    "mid": "中盘股息率",
    "small": "小盘股息率",
    "bond10y": "10年期国债收益率",
    "spread": "股息率-国债利差",
}

_cache = TTLCache(default_ttl_sec=_CACHE_TTL_S)
_lock = threading.Lock()


def _now_cn() -> datetime:
    return datetime.now(_CN_TZ)


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def parse_sw_rows(payload: Any) -> dict[str, float]:
    """解析申万指数分析响应，返回 {YYYY-MM-DD: 股息率(%)}。"""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    results = data.get("results")
    if not isinstance(results, list):
        return {}
    rows: dict[str, float] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        day = str(item.get("bargaindate") or "").strip()[:10]
        value = _float(item.get("dp"))
        if len(day) == 10 and value is not None and value >= 0:
            rows[day] = value
    return rows


def parse_bond_rows(payload: Any) -> dict[str, float]:
    """解析东方财富中美国债收益率响应，返回 {YYYY-MM-DD: 10Y 收益率(%)}。"""
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    data = result.get("data")
    if not isinstance(data, list):
        return {}
    rows: dict[str, float] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        day = str(item.get("SOLAR_DATE") or "").strip()[:10]
        value = _float(item.get(_BOND_10Y_FIELD))
        if len(day) == 10 and value is not None and value > 0:
            rows[day] = value
    return rows


def _fetch_sw_index(index_type: str, code: str, start: str, end: str) -> dict[str, float]:
    payload = market_get(
        _SW_URL,
        host_key="www.swsresearch.com",
        params={
            "page": "1",
            "page_size": "5000",
            "index_type": index_type,
            "start_date": start,
            "end_date": end,
            "type": "DAY",
            "swindexcode": code,
        },
        headers=_SW_HEADERS,
        parse="json",
        timeout=30.0,
        total_timeout=60.0,
        verify=False,
        symbol=f"SW-{code}",
        log_label="申万宏源指数分析",
    )
    return parse_sw_rows(payload)


def _fetch_bond_rows(start: str, end: str) -> dict[str, float]:
    rows: dict[str, float] = {}
    for page in range(1, _MAX_BOND_PAGES + 1):
        payload = market_get(
            _BOND_URL,
            host_key="datacenter.eastmoney.com",
            params={
                "type": "RPTA_WEB_TREASURYYIELD",
                "sty": "ALL",
                "st": "SOLAR_DATE",
                "sr": "-1",
                "token": _BOND_TOKEN,
                "p": page,
                "ps": 500,
            },
            headers=_BOND_HEADERS,
            parse="json",
            timeout=20.0,
            total_timeout=40.0,
            symbol="EM-TREASURY",
            log_label="东方财富国债收益率",
        )
        page_rows = parse_bond_rows(payload)
        if not page_rows:
            break
        rows.update(row for row in page_rows.items() if start <= row[0] <= end)
        if min(page_rows) < start:
            break
    return rows


def load_rows(*, force: bool = False) -> dict[str, Any]:
    if not force:
        cached = _cache.get("rows")
        if isinstance(cached, dict):
            return cached
    with _lock:
        if not force:
            cached = _cache.get("rows")
            if isinstance(cached, dict):
                return cached
        end = _now_cn().date()
        start = end - timedelta(days=_LOOKBACK_DAYS)
        start_text, end_text = start.isoformat(), end.isoformat()
        equity: dict[str, dict[str, float]] = {}
        for key, code, index_type in _SW_INDEXES:
            try:
                rows = _fetch_sw_index(index_type, code, start_text, end_text)
            except Exception as exc:
                logger.warning("[股息率] 申万 %s 抓取失败: %s", code, exc)
                rows = {}
            if rows:
                equity[key] = rows
        bond = _fetch_bond_rows(start_text, end_text)
        result = {"equity": equity, "bond": bond, "start": start_text, "end": end_text}
        if equity:
            _cache.set("rows", result)
        return result


def _align_bond(dates: list[str], bond: dict[str, float]) -> list[float | None]:
    bond_dates = sorted(bond)
    aligned: list[float | None] = []
    for day in dates:
        index = bisect_right(bond_dates, day) - 1
        aligned.append(bond[bond_dates[index]] if index >= 0 else None)
    return aligned


def _prior_value(
    dates: list[str], values: list[float | None], latest_date: str
) -> float | None:
    target = (
        date.fromisoformat(latest_date) - timedelta(days=_PRIOR_DAYS)
    ).isoformat()
    index = bisect_right(dates, target) - 1
    for position in range(index, -1, -1):
        value = values[position]
        if value is not None:
            return value
    return None


def _percentile(values: list[float | None], latest: float | None) -> float | None:
    if latest is None:
        return None
    finite = [value for value in values if value is not None]
    if not finite:
        return None
    return round(sum(1 for value in finite if value <= latest) / len(finite) * 100, 1)


def _build_metric(
    key: str,
    dates: list[str],
    values: list[float | None],
) -> dict[str, Any] | None:
    latest: float | None = None
    latest_date: str | None = None
    for index in range(len(values) - 1, -1, -1):
        value = values[index]
        if value is not None:
            latest = value
            latest_date = dates[index]
            break
    if latest is None or latest_date is None:
        return None
    prior = _prior_value(dates, values, latest_date)
    delta = round(latest - prior, 4) if prior is not None else None
    delta_pct = (
        round(delta / prior * 100, 4)
        if delta is not None and prior and key != "spread"
        else None
    )
    return {
        "id": key,
        "label": _SERIES_LABELS[key],
        "unit": "pp" if key == "spread" else "%",
        "axis": "left",
        "latest": round(latest, 4),
        "latest_date": latest_date,
        "prior": round(prior, 4) if prior is not None else None,
        "delta": delta,
        "delta_pct": delta_pct,
        "percentile": _percentile(values, latest),
    }


def _empty_payload() -> dict[str, Any]:
    return {
        "group": DIVIDEND_YIELD_GROUP_ID,
        "group_label": "A股股息率",
        "scope": "A股整体与大小盘风格股息率、10年期国债收益率及利差（近5年）",
        "as_of": None,
        "dates": [],
        "metrics": [],
        "series": {},
        "sources": [
            {
                "id": "swsresearch",
                "name": "申万宏源研究 · 指数分析",
                "url": _SW_URL,
                "cadence": "日度",
                "start": None,
                "latest": None,
            },
            {
                "id": "eastmoney",
                "name": "东方财富 · 中美国债收益率",
                "url": "https://data.eastmoney.com/cjsj/zmgzsyl.html",
                "cadence": "日度",
                "start": None,
                "latest": None,
            },
        ],
        "coverage": {"trading_days": 0, "target": 0, "complete": True},
    }


def build_payload(
    equity: dict[str, dict[str, float]], bond: dict[str, float]
) -> dict[str, Any]:
    if not equity:
        return _empty_payload()

    dates = sorted({day for rows in equity.values() for day in rows})
    if not dates:
        return _empty_payload()

    series: dict[str, list[float | None]] = {}
    for key, _, _ in _SW_INDEXES:
        rows = equity.get(key)
        if rows:
            series[key] = [rows.get(day) for day in dates]

    bond_values = _align_bond(dates, bond)
    if bond and any(value is not None for value in bond_values):
        series["bond10y"] = bond_values
        all_values = series.get("all")
        if all_values:
            series["spread"] = [
                round(all_value - bond_value, 4)
                if all_value is not None and bond_value is not None
                else None
                for all_value, bond_value in zip(all_values, bond_values)
            ]

    metrics = [
        metric
        for key in ("all", "large", "mid", "small", "bond10y", "spread")
        if key in series
        and (metric := _build_metric(key, dates, series[key])) is not None
    ]

    bond_dates = sorted(bond)
    return {
        "group": DIVIDEND_YIELD_GROUP_ID,
        "group_label": "A股股息率",
        "scope": "A股整体与大小盘风格股息率、10年期国债收益率及利差（近5年）",
        "as_of": dates[-1],
        "dates": dates,
        "metrics": metrics,
        "series": series,
        "sources": [
            {
                "id": "swsresearch",
                "name": "申万宏源研究 · 指数分析",
                "url": _SW_URL,
                "cadence": "日度",
                "start": dates[0],
                "latest": dates[-1],
            },
            {
                "id": "eastmoney",
                "name": "东方财富 · 中美国债收益率",
                "url": "https://data.eastmoney.com/cjsj/zmgzsyl.html",
                "cadence": "日度",
                "start": bond_dates[0] if bond_dates else None,
                "latest": bond_dates[-1] if bond_dates else None,
            },
        ],
        "coverage": {
            "trading_days": len(dates),
            "target": len(dates),
            "complete": True,
        },
    }


def get_curve_payload(
    group_id: str,
    days: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    _require_group(group_id)
    rows = load_rows(force=force)
    return build_payload(rows.get("equity") or {}, rows.get("bond") or {})
