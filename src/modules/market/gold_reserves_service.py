"""中国官方黄金储备：国家外汇管理局（SAFE）月度披露的实物持有量。

SAFE 在「外汇储备」栏目按年归档「官方储备资产」表格，黄金一行同时给出美元
估值与数量。本服务从年度索引页发现各年表格页，取「4. 黄金 Gold」项下的数量
行（万盎司，美元与 SDR 两列重复），按 1 万盎司 = 0.311034768 吨折算为吨。
缺失月份不补值；早于 2018 年的年度页只提供 Excel/PDF 附件，未纳入。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from src.modules.market.etf_share_service import _require_group
from src.platform.marketdata.collectors.market_http import TTLCache, market_get

logger = logging.getLogger(__name__)

GOLD_RESERVES_GROUP_ID = "goldReserves"

_BASE_URL = "https://www.safe.gov.cn"
_INDEX_URL = f"{_BASE_URL}/safe/whcb/index.html"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"}
_YEAR_LINK = re.compile(
    r'href="(/safe/\d{4}/\d{4}/\d+\.html)"[^>]*title="官方储备资产（(\d{4})年）"'
)
_TABLE_LINK = re.compile(r'href="(/safe/\d{4}/\d{4}/\d+\.html)"')
_MONTH_CELL = re.compile(r"(\d{4})\.(\d{2})")
_QUANTITY = re.compile(r"([\d,]+(?:\.\d+)?)\s*万盎司")
_TAG = re.compile(r"<[^>]+>")

_WAN_OUNCE_TO_TONNE = 0.311034768
_CACHE_TTL_S = 6 * 3600

_cache = TTLCache(default_ttl_sec=_CACHE_TTL_S)
_lock = threading.Lock()


def _clean_cell(raw: str) -> str:
    text = _TAG.sub("", raw)
    return text.replace("&nbsp;", " ").replace("\xa0", " ").strip()


def parse_yearly_links(html: Any) -> list[tuple[int, str]]:
    """从年度索引页提取「官方储备资产（YYYY年）」的文章链接，按年份升序去重。"""
    if not isinstance(html, str):
        return []
    links: dict[int, str] = {}
    for match in _YEAR_LINK.finditer(html):
        year = int(match.group(2))
        links.setdefault(year, match.group(1))
    return sorted(links.items())


def parse_table_links(html: Any) -> list[str]:
    """文章页里的表格页候选链接（同一站点、去重、保持出现顺序）。"""
    if not isinstance(html, str):
        return []
    links: list[str] = []
    for href in _TABLE_LINK.findall(html):
        if href not in links:
            links.append(href)
    return links


def parse_gold_table(html: Any, year: int) -> dict[str, float]:
    """解析年度表格页，返回 {YYYY-MM: 万盎司}；月份列按美元/SDR 成对出现。"""
    if not isinstance(html, str):
        return {}
    months: list[str] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [
            _clean_cell(cell)
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
        ]
        if not months:
            found = [
                f"{int(match.group(1))}-{int(match.group(2)):02d}"
                for cell in cells
                if (match := _MONTH_CELL.fullmatch(cell))
            ]
            if len(found) >= 12:
                months = found[:12]
            continue
        if not any("万盎司" in cell for cell in cells):
            continue
        values: dict[str, float] = {}
        for index, month in enumerate(months):
            if not month.startswith(f"{year}-"):
                continue
            position = 1 + index * 2
            cell = cells[position] if position < len(cells) else ""
            match = _QUANTITY.search(cell)
            if match is None:
                continue
            value = float(match.group(1).replace(",", ""))
            if value > 0:
                values[month] = value
        return values
    return {}


def _fetch_text(path: str, symbol: str) -> str:
    return market_get(
        path if path.startswith("http") else f"{_BASE_URL}{path}",
        host_key="www.safe.gov.cn",
        headers=_HEADERS,
        parse="text",
        timeout=25.0,
        total_timeout=50.0,
        symbol=symbol,
        log_label="外管局黄金储备",
    )


def _fetch_year(year: int, article_path: str) -> dict[str, float]:
    article = _fetch_text(article_path, symbol=f"SAFE-{year}")
    for candidate in parse_table_links(article):
        if candidate == article_path:
            continue
        rows = parse_gold_table(_fetch_text(candidate, symbol=f"SAFE-{year}"), year)
        if rows:
            return rows
    return {}


def _fetch_rows() -> dict[str, float]:
    index = _fetch_text(_INDEX_URL, symbol="SAFE-index")
    rows: dict[str, float] = {}
    for year, path in parse_yearly_links(index):
        try:
            rows.update(_fetch_year(year, path))
        except Exception as exc:  # 单年失败不影响其它年份
            logger.warning("[黄金储备] %s 年数据抓取失败: %s", year, exc)
    return rows


def load_rows(*, force: bool = False) -> dict[str, float]:
    if not force:
        cached = _cache.get("rows")
        if isinstance(cached, dict):
            return cached
    with _lock:
        if not force:
            cached = _cache.get("rows")
            if isinstance(cached, dict):
                return cached
        rows = _fetch_rows()
        if not rows:
            time.sleep(1.0)
            rows = _fetch_rows()
        if rows:
            _cache.set("rows", rows)
        return rows


def _prior_month(month: str) -> str:
    year, number = int(month[:4]), int(month[5:7])
    if number == 1:
        return f"{year - 1}-12"
    return f"{year}-{number - 1:02d}"


def build_payload(rows: dict[str, float]) -> dict[str, Any]:
    group = _require_group(GOLD_RESERVES_GROUP_ID)
    dates = sorted(rows)
    values = [round(rows[month] * _WAN_OUNCE_TO_TONNE, 2) for month in dates]
    metrics: list[dict[str, Any]] = []
    if dates:
        latest_date = dates[-1]
        latest = values[-1]
        prior_date = _prior_month(latest_date)
        prior = values[dates.index(prior_date)] if prior_date in dates else None
        delta = round(latest - prior, 2) if prior is not None else None
        delta_pct = (
            round(delta / prior * 100, 4)
            if delta is not None and prior
            else None
        )
        metrics.append(
            {
                "id": "holdings",
                "label": "黄金储备",
                "unit": "吨",
                "axis": "left",
                "latest": latest,
                "latest_date": latest_date,
                "prior": prior,
                "delta": delta,
                "delta_pct": delta_pct,
            }
        )
    sources = [
        {
            "id": "safe",
            "name": "国家外汇管理局 · 官方储备资产",
            "url": _INDEX_URL,
            "cadence": "月度",
            "start": dates[0] if dates else None,
            "latest": dates[-1] if dates else None,
        }
    ]
    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "中国官方黄金储备月末值（吨）",
        "unit": "吨",
        "as_of": dates[-1] if dates else None,
        "dates": dates,
        "metrics": metrics,
        "series": {"holdings": values},
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
    return build_payload(load_rows(force=force))
