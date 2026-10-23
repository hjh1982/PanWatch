"""电解铝/氧化铝：A00 铝与山西氧化铝日度现货价。"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.modules.market.etf_share_service import _require_group, today_cn
from src.platform.marketdata.collectors.market_http import market_get
from src.platform.persistence.models import AluminumPricePoint

logger = logging.getLogger(__name__)

ALUMINUM_GROUP_ID = "aluminum"
_CN_TZ = ZoneInfo("Asia/Shanghai")
_CNAL_URL = "https://market.cnal.com/historical/search.html"
_CNAL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://market.cnal.com/historical/cj.html",
}
_CNAL_SELECT_ID = "3"  # 有色宝长江「铝」
_PPI_LIST_URL = "https://www.100ppi.com/news/list-12--62-{page}.html"
_PPI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://al.100ppi.com/",
}
_MAX_PPI_PAGES = 10
_ARTICLE_WORKERS = 6
_SYNC_INTERVAL_S = 6 * 3600

_CNAL_ROW = re.compile(
    r"<tr><td>铝</td><td>([0-9.]+)-([0-9.]+)</td><td>([0-9.]+)</td>"
    r"<td>([-+]?[0-9.]*)</td>\s*<td>(\d{2})-(\d{2})-(\d{2})</td>"
)
_PPI_LINK = re.compile(
    r'<a href="(https://www\.100ppi\.com/news/detail-(\d{8})-\d+\.html)"'
    r'[^>]*class="blueq">([^<]*中铝氧化铝[^<]*)</a>'
)
_PPI_SHANXI = re.compile(r"山西地区氧化铝现货对外报价\s*([0-9]+(?:\.[0-9]+)?)\s*元/吨")

_state = {"last_sync": 0.0, "lock": threading.Lock()}


@dataclass(frozen=True, slots=True)
class A00Quote:
    low: float
    high: float
    avg: float
    change: float | None


def _to_float(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _resolve_year(month: int, day: int, today: date) -> date | None:
    for year in (today.year, today.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate <= today + timedelta(days=7):
            return candidate
    return None


def parse_cnal_html(html: str, today: date | None = None) -> dict[str, A00Quote]:
    """解析世铝网历史查询表格，返回按日期升序的 A00 报价。"""
    current = today or today_cn()
    rows: dict[str, A00Quote] = {}
    for match in _CNAL_ROW.finditer(html):
        low = _to_float(match.group(1))
        high = _to_float(match.group(2))
        avg = _to_float(match.group(3))
        if low is None or high is None or avg is None:
            continue
        resolved = _resolve_year(int(match.group(6)), int(match.group(7)), current)
        if resolved is None:
            continue
        rows[resolved.isoformat()] = A00Quote(
            low=low,
            high=high,
            avg=avg,
            change=_to_float(match.group(4)),
        )
    return dict(sorted(rows.items()))


def fetch_cnal_a00(start: date, end: date) -> dict[str, A00Quote]:
    payload = {
        "starttime": start.isoformat(),
        "endtime": end.isoformat(),
        "selectid": _CNAL_SELECT_ID,
    }
    with httpx.Client(
        headers=_CNAL_HEADERS,
        timeout=20.0,
        follow_redirects=True,
        trust_env=True,
    ) as client:
        response = client.post(_CNAL_URL, data=payload)
        response.raise_for_status()
    return parse_cnal_html(response.text, today=end)


def parse_ppi_alumina(html: str) -> float | None:
    match = _PPI_SHANXI.search(html)
    if not match:
        return None
    value = _to_float(match.group(1))
    return value if value and value > 0 else None


def _fetch_ppi_article(url: str) -> str | None:
    return market_get(
        url,
        host_key="www.100ppi.com",
        headers=_PPI_HEADERS,
        min_interval_s=0.12,
        timeout=12.0,
        total_timeout=30.0,
        retries=1,
        log_label="aluminum-alumina",
    )


def fetch_ppi_alumina(
    cutoff: date,
    skip_days: set[str] | None = None,
) -> dict[str, float]:
    """抓取生意社「中铝氧化铝现货价格」中的山西报价。"""
    pending: dict[str, str] = {}
    skipped = skip_days or set()
    for page in range(1, _MAX_PPI_PAGES + 1):
        html = market_get(
            _PPI_LIST_URL.format(page=page),
            host_key="www.100ppi.com",
            headers=_PPI_HEADERS,
            min_interval_s=0.2,
            timeout=12.0,
            total_timeout=30.0,
            retries=1,
            log_label="aluminum-alumina-list",
        )
        if not html:
            break
        page_days: list[str] = []
        for match in _PPI_LINK.finditer(html):
            raw_day = match.group(2)
            try:
                day = datetime.strptime(raw_day, "%Y%m%d").date().isoformat()
            except ValueError:
                continue
            page_days.append(day)
            if day < cutoff.isoformat() or day in skipped:
                continue
            pending.setdefault(day, match.group(1))
        if page_days and min(page_days) < cutoff.isoformat():
            break

    if not pending:
        return {}

    values: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=_ARTICLE_WORKERS) as pool:
        futures = {
            day: pool.submit(_fetch_ppi_article, url)
            for day, url in sorted(pending.items())
        }
        for day, future in futures.items():
            try:
                html = future.result()
            except Exception:  # noqa: BLE001 - 单篇失败不影响其它日期
                logger.warning("[电解铝] 读取氧化铝报价失败：%s", day)
                continue
            value = parse_ppi_alumina(html or "")
            if value is not None:
                values[day] = value
    return values


def _sync_rows(db: Session, days: int, *, force: bool) -> None:
    today = today_cn()
    lookback = min(330, max(90, days * 2 + 30))
    start = today - timedelta(days=lookback)

    a00: dict[str, A00Quote] = {}
    try:
        a00 = fetch_cnal_a00(start, today)
    except Exception:  # noqa: BLE001 - 网络异常时保留已有数据
        logger.warning("[电解铝] 读取 A00 铝报价失败", exc_info=True)

    existing_alumina = {
        str(day)
        for (day,) in db.query(AluminumPricePoint.trade_date)
        .filter(AluminumPricePoint.alumina_avg.isnot(None))
        .all()
    }
    try:
        alumina = fetch_ppi_alumina(start, existing_alumina)
    except Exception:  # noqa: BLE001
        logger.warning("[电解铝] 读取氧化铝报价失败", exc_info=True)
        alumina = {}

    if not a00 and not alumina:
        return

    start_day = min([*a00, *alumina])
    records = {
        str(record.trade_date): record
        for record in db.query(AluminumPricePoint)
        .filter(AluminumPricePoint.trade_date >= start_day)
        .all()
    }
    for day, quote in a00.items():
        record = records.get(day)
        if record is None:
            record = AluminumPricePoint(trade_date=day)
            db.add(record)
            records[day] = record
        record.a00_low = quote.low
        record.a00_high = quote.high
        record.a00_avg = quote.avg
        record.a00_change = quote.change
    for day, value in alumina.items():
        record = records.get(day)
        if record is None:
            record = AluminumPricePoint(trade_date=day)
            db.add(record)
            records[day] = record
        record.alumina_avg = value
    db.commit()


def _needs_sync(db: Session, days: int) -> bool:
    if time.monotonic() - _state["last_sync"] < _SYNC_INTERVAL_S:
        return False
    latest = db.query(func.max(AluminumPricePoint.trade_date)).scalar()
    if latest is None:
        return True
    if str(latest) < today_cn().isoformat():
        return True
    count = db.query(func.count(AluminumPricePoint.id)).scalar() or 0
    return count < days


def load_rows(db: Session) -> dict[str, dict[str, float | None]]:
    rows: dict[str, dict[str, float | None]] = {}
    for record in db.query(AluminumPricePoint).order_by(AluminumPricePoint.trade_date):
        rows[str(record.trade_date)] = {
            "a00_low": record.a00_low,
            "a00_high": record.a00_high,
            "a00_avg": record.a00_avg,
            "alumina_avg": record.alumina_avg,
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
) -> dict[str, Any]:
    group = _require_group(ALUMINUM_GROUP_ID)
    all_dates = sorted(rows)
    dates = all_dates[-days:] if days > 0 else all_dates

    a00_avg: list[float | None] = []
    a00_low: list[float | None] = []
    a00_high: list[float | None] = []
    alumina_avg: list[float | None] = []
    alumina_last: float | None = None
    if dates:
        for day in reversed(all_dates):
            if day >= dates[0]:
                continue
            prior = rows.get(day, {}).get("alumina_avg")
            if prior is not None:
                alumina_last = prior
                break
    for day in dates:
        row = rows.get(day, {})
        avg = row.get("a00_avg")
        a00_avg.append(avg)
        a00_low.append(row.get("a00_low"))
        a00_high.append(row.get("a00_high"))
        current_alumina = row.get("alumina_avg")
        if current_alumina is not None:
            alumina_last = current_alumina
        alumina_avg.append(alumina_last)

    spread = [
        round(float(a00) - float(alumina), 4)
        if a00 is not None and alumina is not None
        else None
        for a00, alumina in zip(a00_avg, alumina_avg)
    ]

    metrics = [
        metric
        for metric in (
            _metric("a00_avg", dates, a00_avg),
            _metric("alumina_avg", dates, alumina_avg),
            _metric("spread", dates, spread),
        )
        if metric is not None
    ]
    as_of = max((metric["latest_date"] for metric in metrics), default=None)
    active_days = sum(1 for value in a00_avg if value is not None) or len(dates)

    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "A00铝 · 氧化铝(山西)",
        "as_of": as_of,
        "dates": dates,
        "metrics": metrics,
        "series": {
            "a00_avg": a00_avg,
            "a00_low": a00_low,
            "a00_high": a00_high,
            "alumina_avg": alumina_avg,
            "spread": spread,
        },
        "coverage": {
            "trading_days": active_days,
            "target": days,
            "complete": bool(dates) and len(dates) >= days,
        },
    }


def get_curve_payload(
    db: Session,
    group_id: str,
    days: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    _require_group(group_id)
    with _state["lock"]:
        if force or _needs_sync(db, days):
            _sync_rows(db, days, force=force)
            _state["last_sync"] = time.monotonic()
    return build_payload(load_rows(db), days)
