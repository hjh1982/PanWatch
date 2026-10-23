"""铝锭库存：Mysteel 电解铝社会库存与铝厂厂内库存。"""

from __future__ import annotations

import html as html_lib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.modules.market.etf_share_service import _require_group, today_cn
from src.platform.marketdata.collectors.market_http import market_get
from src.platform.persistence.models import AluminumInventoryPoint

logger = logging.getLogger(__name__)

ALUMINUM_INVENTORY_GROUP_ID = "aluminumInventory"
_LIST_URL = "https://list1.mysteel.com/article/p-4434----0202---------{page}.html"
_ARTICLE_HOST = "youse.mysteel.com"
_MAX_PAGES = 7
_LOOKBACK_DAYS = 200
_CHANGE_MAX_GAP_DAYS = 10
_ARTICLE_WORKERS = 5
_SYNC_INTERVAL_S = 6 * 3600

_LIST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://youse.mysteel.com/",
}
_ARTICLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://list1.mysteel.com/",
}

_LINK = re.compile(
    r'<a[^>]+href="(https://youse\.mysteel\.com/a/\d+/[0-9A-F]+\.html)"'
    r"[^>]*>(.*?)</a>",
    re.S,
)
_SOCIAL_TITLE = re.compile(
    r"中国主要市场电解铝库存统计[（(]\s*(\d{4})\.(\d{1,2})\.(\d{1,2})"
    r"\s*-\s*(\d{4})\.(\d{1,2})\.(\d{1,2})\s*[）)]"
)
_FACTORY_TITLE = re.compile(
    r"中国电解铝企业厂内铝锭库存统计[（(]\s*"
    r"(\d{4})\.(\d{1,2})\.(\d{1,2})\s*[）)]"
)
_SUMMARY_ATTR = re.compile(r'data-ai-content-summary="([^"]*)"')
_META_DESC = re.compile(r'<meta name="description" content="([^"]*)"')
_TAG = re.compile(r"<[^>]+>")
_RANGE_SUFFIX = (
    r"[^。；;]{0,25}?(?:由|从)\s*[0-9.]+\s*万吨[^。；;]{0,20}?"
    r"(?:降至|增至|升至|涨至|跌至|回落至|反弹至|减少至|增加至)"
    r"\s*([0-9.]+)\s*万吨"
)
_SOCIAL_RANGE_END = re.compile(
    r"(?:社会库存|库存合计|库存总量|总库存|总量|合计)" + _RANGE_SUFFIX
)
_FACTORY_RANGE_END = re.compile(
    r"(?:厂内铝锭库存|厂内库存|库存合计|库存总量|库存总计|总库存|总量|合计)"
    + _RANGE_SUFFIX
)
_SOCIAL_END = re.compile(
    r"(?:库存合计|库存总量|库存总计|总库存|总量|合计|社会库存(?:合计|总量|总计)?)"
    r"\s*(?:为|达|降至|增至|至)?\s*([0-9.]+)\s*万吨"
)
_FACTORY_END = re.compile(
    r"(?:厂内铝锭库存(?:总计|合计|总量)?|厂内库存|库存合计|库存总量|库存总计|总库存|总量|合计)"
    r"\s*(?:为|达|降至|增至|至)?\s*([0-9.]+)\s*万吨"
)

_state = {"last_sync": 0.0, "lock": threading.Lock()}


@dataclass(frozen=True, slots=True)
class InventoryArticle:
    kind: str
    day: str
    url: str


def _to_float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _clean(fragment: str) -> str:
    return html_lib.unescape(_TAG.sub("", fragment)).strip()


def _iso_day(year: str, month: str, day: str) -> str | None:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _summary_text(page_html: str) -> str:
    match = _SUMMARY_ATTR.search(page_html) or _META_DESC.search(page_html)
    return html_lib.unescape(match.group(1)) if match else ""


def _pick_value(
    text: str,
    anchored: re.Pattern[str],
    direct: re.Pattern[str],
    low: float,
    high: float,
) -> float | None:
    if not text:
        return None
    for pattern in (anchored, direct):
        match = pattern.search(text)
        if not match:
            continue
        value = _to_float(match.group(1))
        if value is not None and low <= value <= high:
            return value
    return None


def parse_social_inventory(text: str) -> float | None:
    """解析社会库存万吨数（范围 10-500 万吨）。"""
    return _pick_value(text, _SOCIAL_RANGE_END, _SOCIAL_END, 10, 500)


def parse_factory_inventory(text: str) -> float | None:
    """解析铝厂厂内库存万吨数（范围 0.1-100 万吨）。"""
    return _pick_value(text, _FACTORY_RANGE_END, _FACTORY_END, 0.1, 100)


def _collect_articles(
    cutoff: date,
    skip_social: set[str],
    skip_factory: set[str],
) -> list[InventoryArticle]:
    cutoff_text = cutoff.isoformat()
    seen: dict[tuple[str, str], InventoryArticle] = {}
    for page in range(1, _MAX_PAGES + 1):
        page_html = market_get(
            _LIST_URL.format(page=page),
            host_key="list1.mysteel.com",
            headers=_LIST_HEADERS,
            min_interval_s=0.2,
            timeout=15.0,
            total_timeout=30.0,
            retries=1,
            log_label="aluminum-inventory-list",
        )
        if not page_html:
            break
        page_days: list[str] = []
        for match in _LINK.finditer(page_html):
            title = _clean(match.group(2))
            social = _SOCIAL_TITLE.search(title)
            if social:
                day = _iso_day(social.group(4), social.group(5), social.group(6))
                if day is None:
                    continue
                page_days.append(day)
                if day >= cutoff_text and day not in skip_social:
                    seen.setdefault(
                        ("social", day),
                        InventoryArticle("social", day, match.group(1)),
                    )
                continue
            factory = _FACTORY_TITLE.search(title)
            if not factory:
                continue
            day = _iso_day(factory.group(1), factory.group(2), factory.group(3))
            if day is None:
                continue
            page_days.append(day)
            if day >= cutoff_text and day not in skip_factory:
                seen.setdefault(
                    ("factory", day),
                    InventoryArticle("factory", day, match.group(1)),
                )
        if page_days and min(page_days) < cutoff_text:
            break
    return list(seen.values())


def _fetch_article(article: InventoryArticle) -> float | None:
    page_html = market_get(
        article.url,
        host_key=_ARTICLE_HOST,
        headers=_ARTICLE_HEADERS,
        min_interval_s=0.12,
        timeout=15.0,
        total_timeout=30.0,
        retries=1,
        log_label="aluminum-inventory-article",
    )
    if not page_html:
        return None
    summary = _summary_text(page_html)
    if article.kind == "social":
        return parse_social_inventory(summary)
    return parse_factory_inventory(summary)


def fetch_inventory(
    cutoff: date,
    skip_social: set[str] | None = None,
    skip_factory: set[str] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """抓取指定日期之后的库存文章，返回社会库存与厂内库存序列。"""
    articles = _collect_articles(
        cutoff,
        skip_social or set(),
        skip_factory or set(),
    )
    if not articles:
        return {}, {}
    social: dict[str, float] = {}
    factory: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=_ARTICLE_WORKERS) as pool:
        futures = {article: pool.submit(_fetch_article, article) for article in articles}
        for article, future in futures.items():
            try:
                value = future.result()
            except Exception:  # noqa: BLE001 - 单篇失败不影响其它期次
                logger.warning(
                    "[铝锭库存] 读取文章失败：%s %s", article.kind, article.day
                )
                continue
            if value is None:
                continue
            if article.kind == "social":
                social[article.day] = value
            else:
                factory[article.day] = value
    return social, factory


def _sync_rows(db: Session) -> None:
    cutoff = today_cn() - timedelta(days=_LOOKBACK_DAYS)
    skip_social = {
        str(day)
        for (day,) in db.query(AluminumInventoryPoint.report_date)
        .filter(AluminumInventoryPoint.social_wan.isnot(None))
        .all()
    }
    skip_factory = {
        str(day)
        for (day,) in db.query(AluminumInventoryPoint.report_date)
        .filter(AluminumInventoryPoint.factory_wan.isnot(None))
        .all()
    }
    social, factory = fetch_inventory(cutoff, skip_social, skip_factory)
    if not social and not factory:
        return

    start_day = min([*social, *factory])
    records = {
        str(record.report_date): record
        for record in db.query(AluminumInventoryPoint)
        .filter(AluminumInventoryPoint.report_date >= start_day)
        .all()
    }
    for day, value in social.items():
        record = records.get(day)
        if record is None:
            record = AluminumInventoryPoint(report_date=day)
            db.add(record)
            records[day] = record
        record.social_wan = value
    for day, value in factory.items():
        record = records.get(day)
        if record is None:
            record = AluminumInventoryPoint(report_date=day)
            db.add(record)
            records[day] = record
        record.factory_wan = value
    db.commit()


def _needs_sync(db: Session) -> bool:
    if time.monotonic() - _state["last_sync"] < _SYNC_INTERVAL_S:
        return False
    latest = db.query(func.max(AluminumInventoryPoint.report_date)).scalar()
    if latest is None:
        return True
    freshness_cutoff = today_cn() - timedelta(days=2)
    return str(latest) < freshness_cutoff.isoformat()


def load_rows(db: Session) -> dict[str, dict[str, float | None]]:
    rows: dict[str, dict[str, float | None]] = {}
    for record in db.query(AluminumInventoryPoint).order_by(
        AluminumInventoryPoint.report_date
    ):
        rows[str(record.report_date)] = {
            "social_wan": record.social_wan,
            "factory_wan": record.factory_wan,
        }
    return rows


def _metric(
    metric_id: str,
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
        "id": metric_id,
        "latest": round(float(latest), 4),
        "latest_date": latest_date,
        "delta": delta,
        "delta_pct": delta_pct,
    }


def build_payload(
    rows: dict[str, dict[str, float | None]],
    days: int,
    *,
    full: bool = True,
) -> dict[str, Any]:
    group = _require_group(ALUMINUM_INVENTORY_GROUP_ID)
    all_dates = sorted(rows)
    if full:
        dates = all_dates
    else:
        cutoff = (today_cn() - timedelta(days=days)).isoformat()
        dates = [day for day in all_dates if day >= cutoff]

    social = [rows.get(day, {}).get("social_wan") for day in dates]
    factory = [rows.get(day, {}).get("factory_wan") for day in dates]
    social_change: list[float | None] = []
    previous_social: float | None = None
    previous_day: str | None = None
    for day, value in zip(dates, social):
        if value is None:
            social_change.append(None)
            continue
        gap = (
            (date.fromisoformat(day) - date.fromisoformat(previous_day)).days
            if previous_day is not None
            else None
        )
        social_change.append(
            round(value - previous_social, 4)
            if previous_social is not None
            and gap is not None
            and gap <= _CHANGE_MAX_GAP_DAYS
            else None
        )
        previous_social = value
        previous_day = day

    factory_last: float | None = None
    total: list[float | None] = []
    for social_value, factory_value in zip(social, factory):
        if factory_value is not None:
            factory_last = factory_value
        total.append(
            round(social_value + factory_last, 4)
            if social_value is not None and factory_last is not None
            else None
        )

    metrics = [
        metric
        for metric in (
            _metric("social", dates, social),
            _metric("factory", dates, factory),
            _metric("total", dates, total),
        )
        if metric is not None
    ]
    as_of = max((metric["latest_date"] for metric in metrics), default=None)
    active_days = sum(1 for value in social if value is not None)

    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "Mysteel · 社会库存与厂内库存",
        "as_of": as_of,
        "dates": dates,
        "metrics": metrics,
        "series": {
            "social": social,
            "factory": factory,
            "total": total,
            "social_change": social_change,
        },
        "coverage": {
            "trading_days": active_days,
            "target": max(active_days, 1),
            "complete": bool(active_days),
        },
    }


def get_curve_payload(
    db: Session,
    group_id: str,
    days: int,
    *,
    force: bool = False,
    full: bool = True,
) -> dict[str, Any]:
    _require_group(group_id)
    with _state["lock"]:
        if force or _needs_sync(db):
            _sync_rows(db)
            _state["last_sync"] = time.monotonic()
    return build_payload(load_rows(db), days, full=full)
