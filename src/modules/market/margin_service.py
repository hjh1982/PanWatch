"""融资融券市场情绪：东财沪深两市融资融券历史汇总。"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

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
_PAGE_SIZE = 260
_CACHE_TTL_S = 300

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

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"fetched_at": 0.0, "rows": {}}


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


def fetch_market_rows() -> dict[str, dict[str, float]]:
    payload = market_get(
        _DATACENTER_URL,
        host_key=_DATACENTER_HOST,
        params={
            "reportName": _REPORT,
            "columns": "ALL",
            "sortColumns": "DIM_DATE",
            "sortTypes": "-1",
            "pageSize": str(_PAGE_SIZE),
            "pageNumber": "1",
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
    if not rows:
        logger.warning("[融资融券] 东财全市场历史数据不可用")
    return rows


def _load_rows(*, force: bool) -> dict[str, dict[str, float]]:
    now = time.monotonic()
    with _cache_lock:
        cached: dict[str, dict[str, float]] = _cache["rows"]
        fresh = bool(cached) and now - _cache["fetched_at"] < _CACHE_TTL_S
    if fresh and not force:
        return cached

    rows = fetch_market_rows()
    if rows:
        with _cache_lock:
            _cache["rows"] = rows
            _cache["fetched_at"] = time.monotonic()
        return rows

    with _cache_lock:
        return dict(_cache["rows"] or {})


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
