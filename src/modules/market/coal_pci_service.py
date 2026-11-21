"""喷吹煤现货价格：钢之家（Steelhome）晋城、长治两个产地（日度，元/吨）。

钢之家煤焦频道市场页把现货报价直接内嵌在 HTML 中：
let spotQuotationList = '[{"price_name":"长治","price_id":"K163304",...}]';
每项含 xAxis（日期，倒序）与 yAxis（价格，与日期一一对应）。公开页只提供
最近约半年走势，更早历史需登录会员；这里按解析后的日期合并缓存。

同比取我的钢铁网（Mysteel）同口径喷吹精煤：晋城用兰花集团日度指数
（ID01360504，回溯一年半），长治用每日市场行情里的“同比”表述。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from src.modules.market.etf_share_service import _require_group
from src.modules.market.statistics_store import StatisticsStore, merge_mappings
from src.platform.marketdata.collectors.market_http import market_get

logger = logging.getLogger(__name__)

COAL_PCI_GROUP_ID = "coalPci"
_CACHE_TTL_S = 6 * 3600
_URL = "https://www.steelhome.com/mj/market.php?varietyId=616,c616"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.steelhome.com/mj/",
}
_CACHE_KEY = "steelhome_spot"
_UNIT = "元/吨"
_ASSIGN_RE = re.compile(
    r"(?:let|var|const)\s+spotQuotationList\s*=\s*'(.*?)'\s*;", re.S
)

_MYSTEEL_CHART_URL = "https://api.mysteel.com/publishd/index/chart/dateData"
_MYSTEEL_ARTICLE_LIST_URL = (
    "https://m.mysteel.com/market/p-968-----050203-0--------1.html"
)
_JINCHENG_INDEX_CODE = "ID01360504"
_JINCHENG_CACHE_KEY = "mysteel_jincheng"
_CHANGZHI_CACHE_KEY = "mysteel_changzhi"
_MYSTEEL_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "text/html,application/json,text/plain,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://coal.m.mysteel.com/",
}
_ARTICLE_LINK_RE = re.compile(
    r'<a class="h-list-item h-hot-news"[^>]*href="'
    r"(//coal\.m\.mysteel\.com/m/\d+/[A-Za-z0-9_]+\.html)\"[^>]*>\s*"
    r"<h3>([^<]*)</h3>",
    re.S,
)
_DESCRIPTION_RE = re.compile(r'"description":\s*"([^"]*)"')
_ARTICLE_PRICE_RE = re.compile(r"价格\s*([0-9]+(?:\.[0-9]+)?)\s*元/吨")
_ARTICLE_YOY_RE = re.compile(
    r"同比\s*(持平|上涨|下跌)\s*(?:([0-9]+(?:\.[0-9]+)?)\s*元/吨)?"
)
_PUBLISHED_RE = re.compile(r'"datePublished":\s*"(\d{4}-\d{2}-\d{2})')


@dataclass(frozen=True, slots=True)
class SeriesDef:
    id: str
    label: str
    price_id: str


SERIES: tuple[SeriesDef, ...] = (
    SeriesDef("jincheng", "晋城喷吹煤", "M166208"),
    SeriesDef("changzhi", "长治喷吹煤", "K163304"),
)
_BY_PRICE_ID = {item.price_id: item for item in SERIES}

_store = StatisticsStore(namespace=COAL_PCI_GROUP_ID, default_ttl_sec=_CACHE_TTL_S)
_lock = threading.Lock()


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _to_day(value: Any) -> str | None:
    text = str(value or "").strip().replace("/", "-")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def parse_payload(text: Any) -> dict[str, dict[str, float]]:
    """解析市场页内嵌的 spotQuotationList，按 price_id 取晋城/长治两条序列。"""
    if not isinstance(text, str):
        return {}
    raw = text.strip()
    match = _ASSIGN_RE.search(raw)
    if match is not None:
        raw = match.group(1).strip()
    elif not raw.startswith("["):
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(payload, list):
        return {}
    rows: dict[str, dict[str, float]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        definition = _BY_PRICE_ID.get(str(item.get("price_id") or ""))
        if definition is None:
            continue
        days = item.get("xAxis")
        values = item.get("yAxis")
        if not isinstance(days, list) or not isinstance(values, list):
            continue
        series = rows.setdefault(definition.id, {})
        for raw_day, raw_value in zip(days, values):
            day = _to_day(raw_day)
            value = _to_float(raw_value)
            if day is None or value is None or value <= 0:
                continue
            series[day] = value
    return {
        key: dict(sorted(value.items()))
        for key, value in rows.items()
        if value
    }


def _fetch_rows() -> dict[str, dict[str, float]]:
    text = market_get(
        _URL,
        host_key="www.steelhome.com",
        headers=_HEADERS,
        min_interval_s=0.5,
        timeout=20.0,
        total_timeout=40.0,
        parse="text",
        trust_env=False,
        symbol=",".join(item.price_id for item in SERIES),
        log_label="喷吹煤现货价格",
    )
    if not text:
        return {}
    rows = parse_payload(text)
    if not rows:
        logger.warning("[喷吹煤] 页面无可解析数据: %.120s", text)
    return rows


def load_rows(*, force: bool = False) -> dict[str, dict[str, float]]:
    if not force:
        cached = _store.get(_CACHE_KEY)
        if isinstance(cached, dict):
            return cached
    with _lock:
        if not force:
            cached = _store.get(_CACHE_KEY)
            if isinstance(cached, dict):
                return cached
        previous = _store.get(_CACHE_KEY, allow_stale=True)
        previous = previous if isinstance(previous, dict) else {}
        rows = _fetch_rows()
        if not rows:
            return previous
        merged = merge_mappings(previous, rows)
        merged = {
            key: dict(sorted(value.items()))
            for key, value in merged.items()
            if isinstance(value, dict)
        }
        _store.set(_CACHE_KEY, merged)
        return merged


def parse_mysteel_index(payload: Any) -> dict[str, float]:
    """解析我的钢铁网指数接口，返回按日期升序的晋城喷吹精煤价格。"""
    raw = payload.get("response") if isinstance(payload, dict) else payload
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, list) or not raw:
        return {}
    item = next(
        (
            entry
            for entry in raw
            if isinstance(entry, dict)
            and str(entry.get("indexCode") or "") == _JINCHENG_INDEX_CODE
        ),
        raw[0],
    )
    if not isinstance(item, dict):
        return {}
    days = item.get("xAxis")
    datasets = item.get("datas")
    if not isinstance(days, list) or not isinstance(datasets, list) or not datasets:
        return {}
    first = datasets[0] if isinstance(datasets[0], dict) else {}
    values = first.get("yAxis")
    if not isinstance(values, list):
        return {}
    points: dict[str, float] = {}
    for raw_day, raw_value in zip(days, values):
        day = _to_day(raw_day)
        value = _to_float(raw_value)
        if day is None or value is None or value <= 0:
            continue
        points[day] = value
    return dict(sorted(points.items()))


def parse_changzhi_summary(text: Any) -> dict[str, Any] | None:
    """从长治喷吹煤日评 JSON-LD 里取“同比”金额，换算去年同期价。"""
    if not isinstance(text, str):
        return None
    description = next(
        (
            raw
            for raw in _DESCRIPTION_RE.findall(text)
            if "元/吨" in raw and "同比" in raw
        ),
        None,
    )
    if description is None:
        return None
    price_match = _ARTICLE_PRICE_RE.search(description)
    yoy_match = _ARTICLE_YOY_RE.search(description)
    if price_match is None or yoy_match is None:
        return None
    price = float(price_match.group(1))
    direction = yoy_match.group(1)
    magnitude = float(yoy_match.group(2) or 0)
    if direction == "下跌":
        amount = -magnitude
    elif direction == "上涨":
        amount = magnitude
    else:
        amount = 0.0
    year_ago = price - amount
    if price <= 0 or year_ago <= 0:
        return None
    published = _PUBLISHED_RE.search(text)
    return {
        "date": published.group(1) if published is not None else None,
        "price": round(price, 2),
        "year_ago": round(year_ago, 2),
        "yoy": round(amount, 2),
        "yoy_pct": round(amount / year_ago * 100, 4),
    }


def _fetch_jincheng_index() -> dict[str, float]:
    today = date.today()
    payload = market_get(
        _MYSTEEL_CHART_URL,
        host_key="api.mysteel.com",
        params={
            "indexCodes": _JINCHENG_INDEX_CODE,
            "handleType": '""',
            "startTime": (today - timedelta(days=400)).isoformat(),
            "endTime": today.isoformat(),
        },
        headers=_MYSTEEL_HEADERS,
        min_interval_s=0.5,
        timeout=20.0,
        total_timeout=40.0,
        parse="json",
        trust_env=False,
        symbol=_JINCHENG_INDEX_CODE,
        log_label="喷吹煤同比-晋城指数",
    )
    points = parse_mysteel_index(payload)
    if not points:
        logger.warning("[喷吹煤] 我的钢铁网晋城指数无可解析数据")
    return points


def _fetch_changzhi_summary() -> dict[str, Any] | None:
    listing = market_get(
        _MYSTEEL_ARTICLE_LIST_URL,
        host_key="m.mysteel.com",
        headers=_MYSTEEL_HEADERS,
        min_interval_s=0.5,
        timeout=20.0,
        total_timeout=40.0,
        parse="text",
        trust_env=False,
        symbol="changzhi-pci",
        log_label="喷吹煤同比-长治日评",
    )
    if not listing:
        return None
    article_url = None
    for match in _ARTICLE_LINK_RE.finditer(listing):
        title = match.group(2)
        if "长治" in title and "喷吹煤价格行情" in title:
            article_url = "https:" + match.group(1)
            break
    if article_url is None:
        return None
    article = market_get(
        article_url,
        host_key="coal.m.mysteel.com",
        headers=_MYSTEEL_HEADERS,
        min_interval_s=0.5,
        timeout=20.0,
        total_timeout=40.0,
        parse="text",
        trust_env=False,
        symbol="changzhi-pci",
        log_label="喷吹煤同比-长治日评",
    )
    if not article:
        return None
    summary = parse_changzhi_summary(article)
    if summary is None:
        logger.warning("[喷吹煤] 长治日评未解析到同比: %.120s", article)
    return summary


def _load_cached(
    key: str,
    fetcher,
    *,
    force: bool,
    merge: bool,
) -> Any:
    if not force:
        cached = _store.get(key)
        if cached:
            return cached
    with _lock:
        if not force:
            cached = _store.get(key)
            if cached:
                return cached
        previous = _store.get(key, allow_stale=True)
        if not isinstance(previous, dict):
            previous = {}
        fresh = fetcher()
        if not fresh:
            return previous
        if merge and previous:
            fresh = merge_mappings(previous, fresh)
            fresh = {
                name: dict(sorted(values.items()))
                for name, values in fresh.items()
                if isinstance(values, dict)
            }
        _store.set(key, fresh)
        return fresh


def load_yoy(*, force: bool = False) -> dict[str, Any]:
    """晋城去年同期序列 + 长治去年同期价；任一失败都不影响主曲线。"""
    jincheng = _load_cached(
        _JINCHENG_CACHE_KEY, _fetch_jincheng_index, force=force, merge=True
    )
    changzhi = _load_cached(
        _CHANGZHI_CACHE_KEY, _fetch_changzhi_summary, force=force, merge=False
    )
    return {
        "jincheng": jincheng if isinstance(jincheng, dict) else {},
        "changzhi": changzhi if isinstance(changzhi, dict) else {},
    }


def _nearest_year_ago(
    series: dict[str, float], latest_date: str
) -> tuple[str, float] | None:
    try:
        anchor = date.fromisoformat(latest_date) - timedelta(days=365)
    except ValueError:
        return None
    best: tuple[tuple[int, int], str, float] | None = None
    for day, value in series.items():
        try:
            current = date.fromisoformat(day)
        except ValueError:
            continue
        gap = abs((current - anchor).days)
        if gap > 15:
            continue
        key = (gap, 0 if current <= anchor else 1)
        if best is None or key < best[0]:
            best = (key, day, float(value))
    if best is None:
        return None
    return best[1], best[2]


def _yoy_payload(
    definition: SeriesDef,
    latest_date: str,
    latest: float,
    yoy: dict[str, Any],
) -> dict[str, Any]:
    if definition.id == "jincheng":
        match = _nearest_year_ago(yoy.get("jincheng") or {}, latest_date)
        if match is None:
            return {}
        prior_date, prior_value = match
    elif definition.id == "changzhi":
        summary = yoy.get("changzhi") or {}
        prior_value = _to_float(summary.get("year_ago"))
        prior_date = summary.get("date")
        if prior_value is None or prior_value <= 0:
            return {}
    else:
        return {}
    amount = latest - prior_value
    return {
        "yoy_date": prior_date,
        "year_ago": round(prior_value, 2),
        "yoy": round(amount, 2),
        "yoy_pct": round(amount / prior_value * 100, 4),
    }


def _metric(
    definition: SeriesDef,
    dates: list[str],
    values: list[float | None],
    yoy: dict[str, Any] | None = None,
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
    metric = {
        "id": definition.id,
        "label": definition.label,
        "unit": _UNIT,
        "latest": round(float(latest), 2),
        "latest_date": latest_date,
        "delta": round(latest - previous, 2) if previous is not None else None,
        "delta_pct": (
            round((latest - previous) / previous * 100, 4)
            if previous not in (None, 0)
            else None
        ),
    }
    if yoy:
        metric.update(_yoy_payload(definition, latest_date, float(latest), yoy))
    return metric


def build_payload(
    rows: dict[str, dict[str, float]],
    days: int,
    yoy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    group = _require_group(COAL_PCI_GROUP_ID)
    union = sorted({day for series in rows.values() for day in series})
    dates = union[-max(days, 1) :] if union else []

    series: dict[str, list[float | None]] = {}
    for definition in SERIES:
        points = rows.get(definition.id, {})
        series[definition.id] = [points.get(day) for day in dates]

    metrics = [
        metric
        for metric in (
            _metric(definition, dates, series[definition.id], yoy)
            for definition in SERIES
        )
        if metric is not None
    ]
    as_of = max((metric["latest_date"] for metric in metrics), default=None)
    covered = sum(
        1 for index in range(len(dates)) if any(
            series[key][index] is not None for key in series
        )
    )
    complete = bool(dates) and all(
        values[-1] is not None for values in series.values()
    )

    sources = [
        {
            "id": "steelhome_pci",
            "name": "钢之家 · 喷吹煤现货价格",
            "url": _URL,
            "cadence": "日线",
            "start": union[0] if union else None,
            "latest": as_of,
        }
    ]
    jincheng_yoy = (yoy or {}).get("jincheng") or {}
    if isinstance(jincheng_yoy, dict) and jincheng_yoy:
        sources.append(
            {
                "id": "mysteel_pci_jincheng",
                "name": "我的钢铁网 · 晋城喷吹精煤（同比）",
                "url": "https://coal.m.mysteel.com/ssgc/mlist/pcmjg/01040403/",
                "cadence": "日线",
                "start": min(jincheng_yoy),
                "latest": max(jincheng_yoy),
            }
        )
    changzhi_yoy = (yoy or {}).get("changzhi") or {}
    if isinstance(changzhi_yoy, dict) and changzhi_yoy.get("date"):
        sources.append(
            {
                "id": "mysteel_pci_changzhi",
                "name": "我的钢铁网 · 长治喷吹精煤日评（同比）",
                "url": _MYSTEEL_ARTICLE_LIST_URL,
                "cadence": "日线",
                "start": changzhi_yoy["date"],
                "latest": changzhi_yoy["date"],
            }
        )

    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "钢之家 喷吹煤现货价格 · 晋城/长治（同比：我的钢铁网）",
        "as_of": as_of,
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
    rows = load_rows(force=force)
    try:
        yoy = load_yoy(force=force)
    except Exception:  # noqa: BLE001 - 同比是附加信息，失败不影响主曲线
        logger.exception("[喷吹煤] 同比数据加载失败")
        yoy = {}
    return build_payload(rows, days, yoy)
