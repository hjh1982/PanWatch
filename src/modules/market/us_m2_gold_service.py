"""美国 M2 货币供应 vs 黄金价格：1960 年以来的月度双轴对比。

M2 取美联储 H.6 月度季调序列（经 DBnomics 镜像），原始单位为十亿美元，
折算为万亿美元；金价取世界银行大宗商品月度价格（Pink Sheet）的 Gold 列，
单位为美元/金衡盎司。两条序列按自然月对齐，缺失月份不补值。
"""

from __future__ import annotations

import io
import logging
import math
import re
import threading
import time
from typing import Any, Callable

from src.modules.market.etf_share_service import _require_group
from src.modules.market.statistics_store import StatisticsStore, merge_mappings
from src.platform.marketdata.collectors.market_http import market_get

logger = logging.getLogger(__name__)

US_M2_GOLD_GROUP_ID = "usM2Gold"

_M2_URL = (
    "https://api.db.nomics.world/v22/series/FED/H6_H6_M2/M2.M"
    "?observations=1&metadata=false"
)
_M2_PAGE = "https://api.db.nomics.world/v22/series/FED/H6_H6_M2/M2.M"
_WB_PAGE = "https://www.worldbank.org/en/research/commodity-markets"
_WB_FALLBACK = (
    "https://thedocs.worldbank.org/en/doc/"
    "74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/"
    "CMO-Historical-Data-Monthly.xlsx"
)
_WB_LINK = re.compile(r"https://[^\"'\s]+CMO-Historical-Data-Monthly\.xlsx")
_WB_MONTH = re.compile(r"(\d{4})M(\d{2})")
_ISO_MONTH = re.compile(r"(\d{4})-(\d{2})")
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_CACHE_TTL_S = 6 * 3600
_METRIC_LABELS = {"m2": "M2 货币供应", "gold": "黄金价格"}

_store = StatisticsStore(namespace=US_M2_GOLD_GROUP_ID, default_ttl_sec=_CACHE_TTL_S)
_locks = {key: threading.Lock() for key in _METRIC_LABELS}


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


def _month_key(text: Any) -> str | None:
    """把 1960M01、1960-01、1960-01-31 统一成 YYYY-MM。"""
    if not isinstance(text, str):
        return None
    value = text.strip()
    match = _WB_MONTH.fullmatch(value) or _ISO_MONTH.match(value)
    if match is None:
        return None
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{match.group(1)}-{month:02d}"


def parse_m2_payload(payload: Any) -> dict[str, float]:
    """解析 DBnomics 的 H.6 M2 月度季调序列，十亿美元折算为万亿美元。"""
    try:
        series = payload["series"]["docs"][0]
        periods = series["period"]
        values = series["value"]
    except (KeyError, IndexError, TypeError):
        return {}
    if not isinstance(periods, list) or not isinstance(values, list):
        return {}
    rows: dict[str, float] = {}
    for period, raw in zip(periods, values):
        month = _month_key(period)
        value = _to_float(raw)
        if month is None or value is None or value <= 0:
            continue
        rows[month] = round(value / 1000.0, 4)
    return {month: rows[month] for month in sorted(rows)}


def parse_wb_gold_link(html: Any) -> str | None:
    if not isinstance(html, str):
        return None
    match = _WB_LINK.search(html)
    return match.group(0) if match else None


def parse_wb_gold_workbook(content: bytes | bytearray) -> dict[str, float]:
    """解析世界银行月度价格表，取 Gold 列（美元/金衡盎司）。"""
    if not content:
        return {}
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover - 依赖缺失时降级为空数据
        logger.warning("[M2 vs 黄金] 缺少 openpyxl，无法解析世界银行价格表")
        return {}
    try:
        workbook = load_workbook(
            io.BytesIO(bytes(content)), read_only=True, data_only=True
        )
    except Exception as exc:
        logger.warning("[M2 vs 黄金] 世界银行价格表解析失败: %s", exc)
        return {}
    try:
        if "Monthly Prices" not in workbook.sheetnames:
            return {}
        sheet = workbook["Monthly Prices"]
        gold_column: int | None = None
        rows: dict[str, float] = {}
        for row in sheet.iter_rows(values_only=True):
            if gold_column is None:
                for index, cell in enumerate(row):
                    if isinstance(cell, str) and cell.strip().lower() == "gold":
                        gold_column = index
                        break
                continue
            month = _month_key(row[0] if row else None)
            if month is None:
                continue
            value = _to_float(row[gold_column]) if len(row) > gold_column else None
            if value is None or value <= 0:
                continue
            rows[month] = round(value, 4)
        return {month: rows[month] for month in sorted(rows)}
    finally:
        workbook.close()


def _retry(fetcher: Callable[[], dict[str, float]]) -> dict[str, float]:
    for attempt in range(2):
        rows = fetcher()
        if rows:
            return rows
        if attempt == 0:
            time.sleep(1.0)
    return {}


def _fetch_m2() -> dict[str, float]:
    payload = market_get(
        _M2_URL,
        host_key="api.db.nomics.world",
        headers={"User-Agent": _UA},
        parse="json",
        timeout=30.0,
        total_timeout=60.0,
        symbol="M2SL",
        log_label="美国M2",
    )
    return parse_m2_payload(payload)


def _fetch_gold() -> dict[str, float]:
    page = market_get(
        _WB_PAGE,
        host_key="www.worldbank.org",
        headers={"User-Agent": _UA},
        parse="text",
        timeout=30.0,
        total_timeout=60.0,
        symbol="CMO",
        log_label="世界银行商品价格",
    )
    link = parse_wb_gold_link(page) or _WB_FALLBACK
    content = market_get(
        link,
        host_key="thedocs.worldbank.org",
        headers={"User-Agent": _UA},
        parse="content",
        timeout=60.0,
        total_timeout=120.0,
        symbol="CMO",
        log_label="世界银行商品价格",
    )
    if not isinstance(content, (bytes, bytearray)):
        return {}
    return parse_wb_gold_workbook(content)


def _load_series(
    series_id: str,
    fetcher: Callable[[], dict[str, float]],
    *,
    force: bool,
) -> dict[str, float]:
    if not force:
        cached = _store.get(series_id)
        if isinstance(cached, dict):
            return cached
    with _locks[series_id]:
        if not force:
            cached = _store.get(series_id)
            if isinstance(cached, dict):
                return cached
        previous = _store.get(series_id, allow_stale=True)
        previous = previous if isinstance(previous, dict) else {}
        try:
            rows = _retry(fetcher)
        except Exception as exc:
            logger.warning("[M2 vs 黄金] %s 数据抓取失败: %s", series_id, exc)
            return previous
        if not rows:
            return previous
        merged = dict(sorted(merge_mappings(previous, rows).items()))
        _store.set(series_id, merged)
        return merged


def load_rows(*, force: bool = False) -> dict[str, dict[str, float]]:
    return {
        "m2": _load_series("m2", _fetch_m2, force=force),
        "gold": _load_series("gold", _fetch_gold, force=force),
    }


def _prior_month(month: str) -> str:
    return f"{int(month[:4]) - 1}-{month[5:]}"


def _metric(
    metric_id: str,
    unit: str,
    axis: str,
    dates: list[str],
    values: list[float | None],
) -> dict[str, Any] | None:
    index = next(
        (position for position in range(len(values) - 1, -1, -1) if values[position] is not None),
        None,
    )
    if index is None:
        return None
    latest = values[index]
    latest_date = dates[index]
    prior_month = _prior_month(latest_date)
    prior = (
        values[dates.index(prior_month)]
        if prior_month in dates and values[dates.index(prior_month)] is not None
        else None
    )
    delta = round(latest - prior, 4) if prior is not None else None
    delta_pct = (
        round((latest - prior) / prior * 100, 4)
        if prior not in (None, 0)
        else None
    )
    return {
        "id": metric_id,
        "label": _METRIC_LABELS[metric_id],
        "unit": unit,
        "axis": axis,
        "latest": round(latest, 4),
        "latest_date": latest_date,
        "prior": round(prior, 4) if prior is not None else None,
        "delta": delta,
        "delta_pct": delta_pct,
    }


def build_payload(
    rows: dict[str, dict[str, float]],
    days: int,
) -> dict[str, Any]:
    group = _require_group(US_M2_GOLD_GROUP_ID)
    m2 = rows.get("m2") or {}
    gold = rows.get("gold") or {}
    months = set(m2) | set(gold)
    if m2 and gold:
        start = max(min(m2), min(gold))
        months = {month for month in months if month >= start}
    dates = sorted(months)
    series = {
        "m2": [m2.get(month) for month in dates],
        "gold": [gold.get(month) for month in dates],
    }
    metrics = [
        metric
        for metric in (
            _metric("m2", "万亿美元", "left", dates, series["m2"]),
            _metric("gold", "美元/盎司", "right", dates, series["gold"]),
        )
        if metric is not None
    ]
    covered = sum(
        1
        for left, right in zip(series["m2"], series["gold"])
        if left is not None and right is not None
    )
    common_latest = next(
        (
            dates[position]
            for position in range(len(dates) - 1, -1, -1)
            if series["m2"][position] is not None and series["gold"][position] is not None
        ),
        None,
    )
    complete = bool(dates) and all(
        values[-1] is not None for values in series.values()
    )
    sources = [
        {
            "id": "m2",
            "name": "美联储 H.6 · M2（经 DBnomics 镜像）",
            "url": _M2_PAGE,
            "cadence": "月度",
            "start": next(iter(m2), None),
            "latest": next(reversed(m2), None) if m2 else None,
        },
        {
            "id": "gold",
            "name": "世界银行 · 大宗商品月度价格（Pink Sheet）",
            "url": _WB_PAGE,
            "cadence": "月度",
            "start": next(iter(gold), None),
            "latest": next(reversed(gold), None) if gold else None,
        },
    ]
    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "美国 M2 货币供应（季调）与黄金月度均价（1960 年起）",
        "as_of": common_latest,
        "dates": dates,
        "metrics": metrics,
        "series": series,
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
