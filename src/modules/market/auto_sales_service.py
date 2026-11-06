"""中国乘用车产销：乘联会（CPCA）狭义/广义乘用车月度产量、批发、零售与出口。

乘联会公开图表接口按年返回 12 个月的四项指标（万辆），同时保留上一年同月
数据，正好用于今年与去年的同月对比。接口只提供当前年与上一年，因此不做
按日窗口裁剪，整段全年数据返回给前端做年度叠加。
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from typing import Any

from src.modules.market.etf_share_service import _require_group
from src.modules.market.statistics_store import StatisticsStore, merge_mappings
from src.platform.marketdata.collectors.market_http import market_get

logger = logging.getLogger(__name__)

AUTO_SALES_GROUP_ID = "autoSales"

_CPCA_URL = "http://data.cpcadata.com/api/chartlist"
_CPCA_PAGE = "http://data.cpcadata.com/TotalMarket"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_CACHE_TTL_S = 1800
_YEAR_KEY = re.compile(r"(20\d{2})年")
_MONTH_KEY = re.compile(r"(\d{1,2})月")
_FIELDS = ("production", "wholesale", "retail", "export")
_METRIC_LABELS = {
    "production": "产量",
    "wholesale": "批发销量",
    "retail": "零售销量",
    "export": "出口",
}
_PREFIXES = ("narrow", "wide")
SERIES_IDS: tuple[str, ...] = tuple(
    f"{prefix}_{field}" for prefix in _PREFIXES for field in _FIELDS
)
METRIC_FIELDS: tuple[str, ...] = _FIELDS

_store = StatisticsStore(namespace=AUTO_SALES_GROUP_ID, default_ttl_sec=_CACHE_TTL_S)
_lock = threading.Lock()


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(str(value).strip().replace(",", ""))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _monthly_totals(dataset: dict[str, Any]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for entry in dataset.get("dataList") or []:
        if not isinstance(entry, dict):
            continue
        match = _MONTH_KEY.fullmatch(str(entry.get("month") or "").strip())
        if match is None:
            continue
        total = 0.0
        for key, values in entry.items():
            if not _YEAR_KEY.fullmatch(key) or not isinstance(values, list):
                continue
            for raw in values:
                value = _to_float(raw)
                if value is not None and value > 0:
                    total += value
        if total > 0:
            totals[match.group(1)] = total
    return totals


def _ordered_datasets(
    datasets: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按同月总量判断哪组是广义乘用车（含微客，数值更大）。"""
    first, second = datasets
    first_totals = _monthly_totals(first)
    second_totals = _monthly_totals(second)
    common = first_totals.keys() & second_totals.keys()
    if common:
        first_sum = sum(first_totals[month] for month in common)
        second_sum = sum(second_totals[month] for month in common)
        if first_sum > second_sum:
            return second, first
    return first, second


def parse_cpca_payload(payload: Any) -> dict[str, dict[str, float]]:
    """解析乘联会图表接口：两组数据分别是狭义、广义乘用车。

    每组 dataList 为 12 个月，每月按生产年份给出一行四项指标：
    [产量, 批发销量, 零售销量, 出口]，单位为万辆。广义乘用车包含微客，
    数值不小于狭义口径，因此按总量升序区分两组。
    """
    if not isinstance(payload, list):
        return {}
    datasets = [
        item
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("dataList"), list)
    ]
    if len(datasets) != 2:
        logger.warning("[乘用车产销] 乘联会返回 %s 组数据，预期 2 组", len(datasets))
        return {}
    rows: dict[str, dict[str, float]] = {}
    for dataset, prefix in zip(_ordered_datasets(datasets), _PREFIXES):
        for entry in dataset["dataList"]:
            if not isinstance(entry, dict):
                continue
            match = _MONTH_KEY.fullmatch(str(entry.get("month") or "").strip())
            if match is None:
                continue
            month = int(match.group(1))
            if not 1 <= month <= 12:
                continue
            for key, values in entry.items():
                year_match = _YEAR_KEY.fullmatch(key)
                if year_match is None or not isinstance(values, list):
                    continue
                if len(values) < len(_FIELDS):
                    continue
                month_key = f"{year_match.group(1)}-{month:02d}"
                target = rows.setdefault(month_key, {})
                for field, raw in zip(_FIELDS, values):
                    value = _to_float(raw)
                    if value is None or value <= 0:
                        continue
                    target[f"{prefix}_{field}"] = round(value, 4)
    return {month: rows[month] for month in sorted(rows) if rows[month]}


def _fetch_rows() -> dict[str, dict[str, float]]:
    payload = market_get(
        _CPCA_URL,
        host_key="data.cpcadata.com",
        params={"charttype": "1"},
        headers={"User-Agent": _UA, "Referer": _CPCA_PAGE},
        parse="json",
        symbol="cpca",
        log_label="乘用车产销",
    )
    return parse_cpca_payload(payload)


def load_rows(*, force: bool = False) -> dict[str, dict[str, float]]:
    if not force:
        cached = _store.get("rows")
        if isinstance(cached, dict):
            return cached
    with _lock:
        if not force:
            cached = _store.get("rows")
            if isinstance(cached, dict):
                return cached
        previous = _store.get("rows", allow_stale=True)
        previous = previous if isinstance(previous, dict) else {}
        rows: dict[str, dict[str, float]] = {}
        try:
            for attempt in range(3):
                rows = _fetch_rows()
                if rows:
                    break
                if attempt < 2:
                    time.sleep(1.0 + attempt)
        except Exception as exc:
            logger.warning("[乘用车产销] 数据抓取失败: %s", exc)
            return previous
        if not rows:
            return previous
        merged = dict(sorted(merge_mappings(previous, rows).items()))
        _store.set("rows", merged)
        return merged


def _yoy_metric(
    metric_id: str,
    label: str,
    latest_date: str,
    latest: float,
    prior: float | None,
) -> dict[str, Any]:
    delta = round(latest - prior, 4) if prior is not None else None
    delta_pct = (
        round((latest - prior) / prior * 100, 4)
        if prior not in (None, 0)
        else None
    )
    return {
        "id": metric_id,
        "label": label,
        "unit": "万辆",
        "axis": "left",
        "latest": round(latest, 4),
        "latest_date": latest_date,
        "prior": round(prior, 4) if prior is not None else None,
        "delta": delta,
        "delta_pct": delta_pct,
    }


def _build_metrics(
    dates: list[str],
    series: dict[str, list[float | None]],
    latest_month: str | None,
) -> list[dict[str, Any]]:
    if latest_month is None:
        return []
    prior_month = f"{int(latest_month[:4]) - 1}-{latest_month[5:]}"
    index = dates.index(latest_month)
    prior_index = dates.index(prior_month) if prior_month in dates else None
    metrics: list[dict[str, Any]] = []
    for field in METRIC_FIELDS:
        latest = series[f"narrow_{field}"][index]
        if latest is None:
            continue
        prior = (
            series[f"narrow_{field}"][prior_index]
            if prior_index is not None
            else None
        )
        metrics.append(
            _yoy_metric(field, _METRIC_LABELS[field], latest_month, latest, prior)
        )
    return metrics


def build_payload(
    rows: dict[str, dict[str, float]],
    days: int,
) -> dict[str, Any]:
    group = _require_group(AUTO_SALES_GROUP_ID)
    dates = sorted(rows)
    series = {
        key: [rows[month].get(key) for month in dates] for key in SERIES_IDS
    }
    current_months = [month for month in dates if month[:4] == dates[-1][:4]] if dates else []
    latest_month = current_months[-1] if current_months else None
    complete = bool(dates) and all(
        values[-1] is not None for values in series.values()
    )
    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "乘联会狭义/广义乘用车月度产量、批发、零售与出口（万辆）",
        "as_of": latest_month,
        "dates": dates,
        "metrics": _build_metrics(dates, series, latest_month),
        "series": series,
        "coverage": {
            "trading_days": len(dates),
            "target": len(dates),
            "complete": complete,
        },
        "source": {
            "name": "乘联会（CPCA）",
            "url": _CPCA_PAGE,
            "cadence": "月度",
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
