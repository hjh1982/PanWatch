"""融资融券市场情绪：东财沪深两市融资融券历史汇总。"""

from __future__ import annotations

import logging
import math
import threading
from typing import Any

from src.modules.market.statistics_store import StatisticsStore, merge_mappings

from src.modules.market.etf_share_service import (
    _require_group,
    target_trade_dates,
    today_cn,
)
from src.platform.marketdata.collectors.market_http import market_get

logger = logging.getLogger(__name__)

MARGIN_GROUP_ID = "margin"
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_DATACENTER_HOST = "datacenter-web.eastmoney.com"
_REPORT = "RPTA_RZRQ_LSHJ"
_HEADERS = {
    "Referer": "https://data.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36"
    ),
}
_YI = 100_000_000
_PAGE_SIZE = 800
_CACHE_TTL_S = 300
_BACKFILL_KEY = "backfill"
_BACKFILL_TTL_S = 365 * 86400.0

# 回撤指标：滚动约 1 年（250 个交易日）高点为基准。
_DRAWDOWN_METRIC = "total_balance"
_DRAWDOWN_WINDOW = 250
_SLOW_DAYS = 20
_FAST_DAYS = 5
_MAX_POINTS = 2500
_NEAR_HIGH_PCT = -2.0

# (指标 id, 东财字段)；金额与融券余量统一换算为亿。
_SERIES: tuple[tuple[str, str], ...] = (
    ("rz_balance", "RZYE"),
    ("rz_buy", "RZMRE"),
    ("rz_repay", "RZCHE"),
    ("rq_volume", "RQYL"),
    ("rq_balance", "RQYE"),
    ("total_balance", "RZRQYE"),
)
_METRIC_IDS = tuple(metric_id for metric_id, _ in _SERIES)

_lock = threading.Lock()
_store = StatisticsStore(MARGIN_GROUP_ID, _CACHE_TTL_S)


def _to_float(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_market_payload(payload: Any) -> dict[str, dict[str, float]]:
    """解析东财全市场两融历史，按交易日返回各指标（亿元 / 亿股）。"""
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    raw_rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(raw_rows, list):
        return {}

    rows: dict[str, dict[str, float]] = {}
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        day = str(raw.get("DIM_DATE") or "")[:10]
        if not day:
            continue
        values: dict[str, float] = {}
        for metric_id, field in _SERIES:
            number = _to_float(raw.get(field))
            if number is not None:
                values[metric_id] = number / _YI
        if values:
            rows[day] = values
    return rows


def _fetch_page(page_number: int) -> tuple[dict[str, dict[str, float]], int]:
    """抓取一页历史，返回（按交易日的行数据, 总页数）。东财单页最多 800 条。"""
    payload = market_get(
        _DATACENTER_URL,
        host_key=_DATACENTER_HOST,
        params={
            "reportName": _REPORT,
            "columns": "ALL",
            "sortColumns": "DIM_DATE",
            "sortTypes": "-1",
            "pageSize": str(_PAGE_SIZE),
            "pageNumber": str(page_number),
            "source": "WEB",
            "client": "WEB",
        },
        headers=_HEADERS,
        parse="json",
        timeout=12,
        retries=1,
        total_timeout=30,
        min_interval_s=0.5,
        log_label="融资融券市场历史",
    )
    rows = parse_market_payload(payload)
    result = payload.get("result") if isinstance(payload, dict) else None
    pages = int(result.get("pages") or 1) if isinstance(result, dict) else 1
    return rows, max(1, pages)


def fetch_history(*, full: bool) -> tuple[dict[str, dict[str, float]], bool]:
    """抓取历史。full=True 时按页拉全量，返回（行数据, 是否取完所有页）。"""
    rows, pages = _fetch_page(1)
    if not rows:
        logger.warning("[融资融券] 东财全市场历史数据不可用")
        return {}, False
    if full and pages > 1:
        for page_number in range(2, pages + 1):
            page_rows, _ = _fetch_page(page_number)
            if not page_rows:
                logger.warning(
                    "[融资融券] 全量回填第 %s/%s 页为空，本次保留已取得部分",
                    page_number,
                    pages,
                )
                return rows, False
            rows.update(page_rows)
    return rows, True


def _load_rows(*, force: bool) -> dict[str, dict[str, float]]:
    with _lock:
        if not force:
            cached = _store.get("rows")
            if cached is not None:
                return cached
        previous = _store.get("rows", allow_stale=True) or {}
        full = _store.get(_BACKFILL_KEY, allow_stale=True) is None
        try:
            rows, complete = fetch_history(full=full)
        except Exception as exc:
            logger.warning("[融资融券] 更新失败，沿用本地数据: %s", exc)
            rows, complete = {}, False
        if rows:
            merged = merge_mappings(previous, rows)
            _store.set("rows", merged)
            if full and complete:
                _store.set(
                    _BACKFILL_KEY,
                    {"trading_days": len(merged), "page_size": _PAGE_SIZE},
                    ttl_sec=_BACKFILL_TTL_S,
                )
            return merged
        return previous


def _classify_state(
    drawdown_pct: float | None,
    velocity_slow: float | None,
    velocity_fast: float | None,
) -> str:
    if drawdown_pct is None:
        return "no_data"
    if drawdown_pct >= _NEAR_HIGH_PCT:
        return "near_high"
    if velocity_slow is None:
        return "unknown"
    if velocity_slow >= 0:
        return "recovering"
    if velocity_fast is not None and velocity_fast < velocity_slow:
        return "accelerating"
    return "decelerating"


def build_drawdown(rows: dict[str, dict[str, float]]) -> dict[str, Any]:
    """相对滚动 1 年高点的回撤与下杀/修复速度（百分点）。"""
    dates = sorted(day for day, values in rows.items() if _DRAWDOWN_METRIC in values)
    count = len(dates)
    values = [rows[day][_DRAWDOWN_METRIC] for day in dates]
    drawdown: list[float | None] = [None] * count
    peak_at: list[int | None] = [None] * count

    if count >= _DRAWDOWN_WINDOW:
        for index in range(_DRAWDOWN_WINDOW - 1, count):
            begin = index - _DRAWDOWN_WINDOW + 1
            high, position = values[begin], begin
            for candidate in range(begin + 1, index + 1):
                if values[candidate] >= high:
                    high, position = values[candidate], candidate
            if high > 0:
                drawdown[index] = (values[index] / high - 1) * 100
                peak_at[index] = position

    def velocity(span: int) -> list[float | None]:
        return [
            (
                round((drawdown[index] - drawdown[index - span]) / span, 6)
                if index >= span
                and drawdown[index] is not None
                and drawdown[index - span] is not None
                else None
            )
            for index in range(count)
        ]

    slow = velocity(_SLOW_DAYS)
    fast = velocity(_FAST_DAYS)

    latest: dict[str, Any] | None = None
    if count and drawdown[count - 1] is not None:
        index = count - 1
        position = peak_at[index] or 0
        high_value = values[position]
        cycle = [
            (day, value)
            for day, value in enumerate(drawdown[position:], start=position)
            if value is not None
        ]
        trough_day, trough_value = min(cycle, key=lambda item: item[1])
        current_value = values[index]
        latest = {
            "date": dates[index],
            "value": round(current_value, 6),
            "high_value": round(high_value, 6),
            "high_date": dates[position],
            "drawdown_pct": round(float(drawdown[index]), 4),
            "velocity_slow": slow[index],
            "velocity_fast": fast[index],
            "days_since_high": index - position,
            "max_drawdown_pct": round(trough_value, 4),
            "max_drawdown_date": dates[trough_day],
            "rebound_needed_pct": (
                round((high_value / current_value - 1) * 100, 4) if current_value > 0 else None
            ),
            "state": _classify_state(drawdown[index], slow[index], fast[index]),
        }

    offset = max(0, count - _MAX_POINTS)
    warmup_end = min(count, _DRAWDOWN_WINDOW - 1)
    return {
        "metric": _DRAWDOWN_METRIC,
        "window": _DRAWDOWN_WINDOW,
        "slow_days": _SLOW_DAYS,
        "fast_days": _FAST_DAYS,
        "warmup_days": max(0, warmup_end - offset),
        "history_days": count,
        "dates": dates[offset:],
        "drawdown_pct": [
            None if value is None else round(value, 4) for value in drawdown[offset:]
        ],
        "velocity_slow": slow[offset:],
        "velocity_fast": fast[offset:],
        "latest": latest,
    }


def build_market_payload(
    rows: dict[str, dict[str, float]],
    targets: list[str],
) -> dict[str, Any]:
    group = _require_group(MARGIN_GROUP_ID)
    dates = [
        day
        for day in targets
        if any(metric_id in rows.get(day, {}) for metric_id in _METRIC_IDS)
    ]
    series: dict[str, list[float | None]] = {}
    for metric_id in _METRIC_IDS:
        series[metric_id] = [
            round(rows[day][metric_id], 6) if metric_id in rows.get(day, {}) else None
            for day in dates
        ]

    metrics: list[dict[str, Any]] = []
    for metric_id in _METRIC_IDS:
        present = [
            (day, rows[day][metric_id])
            for day in dates
            if metric_id in rows.get(day, {})
        ]
        if not present:
            continue
        latest_date, latest = present[-1]
        previous = present[-2][1] if len(present) > 1 else None
        delta = round(latest - previous, 6) if previous is not None else None
        delta_pct = (
            round((latest - previous) / previous * 100, 4)
            if previous not in (None, 0)
            else None
        )
        metrics.append(
            {
                "id": metric_id,
                "latest": round(latest, 6),
                "latest_date": latest_date,
                "delta": delta,
                "delta_pct": delta_pct,
            }
        )

    today = today_cn().isoformat()
    required = [day for day in targets if day != today]
    covered = set(dates)
    complete = all(day in covered for day in required) if required else bool(dates)

    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "沪深两市",
        "as_of": max((metric["latest_date"] for metric in metrics), default=None),
        "dates": dates,
        "metrics": metrics,
        "series": series,
        "drawdown": build_drawdown(rows),
        "coverage": {
            "trading_days": len(dates),
            "target": len(targets),
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
    targets = target_trade_dates(days)
    rows = _load_rows(force=force)
    return build_market_payload(rows, targets)
