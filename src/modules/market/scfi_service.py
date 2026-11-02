"""上海出口集装箱运价指数（SCFI）：交易所官方最新一期与周度历史。

上海航运交易所在公开页面只展示最新两周数值，历史多期查询需要登录。本服务
直接读取交易所英文站的 currentIndex JSON 获取最新两期，并从 OpenContainer 对
交易所公布值的周度整理中补齐 2025-05 以来的历史序列；两处按日期合并，官方
数值优先。缺失周（节假日休刊）不补值。
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from typing import Any

from src.modules.market.etf_share_service import _require_group
from src.platform.marketdata.collectors.market_http import TTLCache, market_get

logger = logging.getLogger(__name__)

SCFI_GROUP_ID = "scfi"

_OFFICIAL_URL = "https://en.sse.net.cn/currentIndex?indexName=scfi"
_OFFICIAL_PAGE = "https://www.sse.net.cn/index/singleIndex?indexType=scfi"
_MIRROR_URLS = (
    "https://opencontainer.kr/en/en-index/en-scfi-2025-2026/",
    "https://opencontainer.kr/en/en-index/en-scfi-2024-2025/",
)
_MIRROR_PAGE = _MIRROR_URLS[0]
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}
_WEEK_LINE = re.compile(
    r"(\d{4})/(\d{2})/(\d{2})\s*\n\s*[^\n]{0,30}?([\d,]+\.\d+)\s*→\s*([\d,]+\.\d+)"
)
_SCRIPT = re.compile(r"<script.*?</script>", re.S | re.I)
_STYLE = re.compile(r"<style.*?</style>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")

_CACHE_TTL_S = 6 * 3600
_cache = TTLCache(default_ttl_sec=_CACHE_TTL_S)
_lock = threading.Lock()


def parse_official(payload: Any) -> dict[str, float]:
    """解析交易所 currentIndex JSON，返回最新两期 {YYYY-MM-DD: 点}。"""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    current_date = str(data.get("currentDate") or "").strip()
    last_date = str(data.get("lastDate") or "").strip()
    current_value: float | None = None
    last_value: float | None = None
    for item in data.get("lineDataList") or []:
        if not isinstance(item, dict):
            continue
        if item.get("dataItemTypeName") != "SCFI_T":
            continue
        current_value = _positive_float(item.get("currentContent"))
        last_value = _positive_float(item.get("lastContent"))
        break
    rows: dict[str, float] = {}
    if last_date and last_value is not None:
        rows[last_date] = last_value
    if current_date and current_value is not None:
        rows[current_date] = current_value
    return rows


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_weekly_rows(page: Any) -> dict[str, float]:
    """从 OpenContainer 周度整理页提取 {YYYY-MM-DD: 当周指数}。"""
    if not isinstance(page, str):
        return {}
    text = _STYLE.sub("", _SCRIPT.sub("", page))
    text = html.unescape(_TAG.sub("\n", text)).replace("\xa0", " ")
    rows: dict[str, float] = {}
    for match in _WEEK_LINE.finditer(text):
        day = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        value = float(match.group(5).replace(",", ""))
        if value > 0:
            rows[day] = value
    return rows


def _fetch_official() -> dict[str, float]:
    payload = market_get(
        _OFFICIAL_URL,
        host_key="en.sse.net.cn",
        headers=_HEADERS,
        parse="json",
        timeout=20.0,
        total_timeout=40.0,
        symbol="SSE-SCFI",
        log_label="上海航运交易所 SCFI",
    )
    return parse_official(payload)


def _fetch_mirror() -> dict[str, float]:
    rows: dict[str, float] = {}
    for url in _MIRROR_URLS:
        try:
            page = market_get(
                url,
                host_key="opencontainer.kr",
                headers=_HEADERS,
                parse="text",
                timeout=25.0,
                total_timeout=50.0,
                symbol="SCFI-history",
                log_label="SCFI 周度历史",
            )
            rows.update(parse_weekly_rows(page))
        except Exception as exc:  # 单页失败不影响另一页
            logger.warning("[SCFI] 周度历史抓取失败 %s: %s", url, exc)
    return rows


def _fetch_snapshot() -> dict[str, Any]:
    mirror: dict[str, float] = {}
    official: dict[str, float] = {}
    try:
        mirror = _fetch_mirror()
    except Exception as exc:
        logger.warning("[SCFI] 周度历史抓取失败: %s", exc)
    try:
        official = _fetch_official()
    except Exception as exc:
        logger.warning("[SCFI] 官方最新一期抓取失败: %s", exc)
    rows = dict(mirror)
    rows.update(official)
    return {"rows": rows, "mirror": mirror, "official": official}


def _load_snapshot(*, force: bool = False) -> dict[str, Any]:
    if not force:
        cached = _cache.get("snapshot")
        if isinstance(cached, dict):
            return cached
    with _lock:
        if not force:
            cached = _cache.get("snapshot")
            if isinstance(cached, dict):
                return cached
        snapshot = _fetch_snapshot()
        if not snapshot["rows"]:
            time.sleep(1.0)
            snapshot = _fetch_snapshot()
        if snapshot["rows"]:
            _cache.set("snapshot", snapshot)
        return snapshot


def build_payload(
    rows: dict[str, float],
    *,
    mirror: dict[str, float] | None = None,
    official: dict[str, float] | None = None,
) -> dict[str, Any]:
    group = _require_group(SCFI_GROUP_ID)
    dates = sorted(rows)
    values = [round(rows[day], 2) for day in dates]
    metrics: list[dict[str, Any]] = []
    if dates:
        latest_date = dates[-1]
        latest = values[-1]
        prior = values[-2] if len(values) > 1 else None
        delta = round(latest - prior, 2) if prior is not None else None
        delta_pct = (
            round(delta / prior * 100, 4)
            if delta is not None and prior
            else None
        )
        metrics.append(
            {
                "id": "scfi",
                "label": "SCFI 综合指数",
                "unit": "点",
                "axis": "left",
                "latest": latest,
                "latest_date": latest_date,
                "prior": prior,
                "delta": delta,
                "delta_pct": delta_pct,
            }
        )
    sources: list[dict[str, Any]] = [
        {
            "id": "sse",
            "name": "上海航运交易所 · SCFI",
            "url": _OFFICIAL_PAGE,
            "cadence": "周度",
            "start": min(official) if official else None,
            "latest": max(official) if official else None,
        }
    ]
    if mirror:
        sources.append(
            {
                "id": "opencontainer",
                "name": "OpenContainer · SCFI 周度整理",
                "url": _MIRROR_PAGE,
                "cadence": "周度",
                "start": min(mirror),
                "latest": max(mirror),
            }
        )
    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "上海出口集装箱运价指数（SCFI）综合指数，周度",
        "unit": "点",
        "as_of": dates[-1] if dates else None,
        "dates": dates,
        "metrics": metrics,
        "series": {"scfi": values},
        "sources": sources,
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
    snapshot = _load_snapshot(force=force)
    return build_payload(
        snapshot["rows"],
        mirror=snapshot["mirror"],
        official=snapshot["official"],
    )
