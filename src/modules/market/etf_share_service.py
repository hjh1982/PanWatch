"""ETF 份额追踪：同步上交所披露的基金规模并生成份额曲线。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.platform.marketdata.collectors.market_http import market_get
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import EtfShareSnapshot, EtfShareTradeDay
from src.platform.scheduling.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")
_SSE_URL = "https://query.sse.com.cn/commonQuery.do"
_SSE_SQL_ID = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
_SSE_HEADERS = {
    "Referer": "https://www.sse.com.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36"
    ),
}
_DEFAULT_DAYS = 90
_ALLOWED_DAYS = (30, 60, 90, 120)
_PUBLISH_HOUR = 16
_TODAY_RETRY_S = 300
_BACKFILL_RETRY_S = 60


@dataclass(frozen=True, slots=True)
class FundDef:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class GroupDef:
    id: str
    label: str
    category: str
    accent: str
    funds: tuple[FundDef, ...] = ()
    available: bool = True


GROUPS: tuple[GroupDef, ...] = (
    GroupDef(
        id="hs300",
        label="沪深300 ETF",
        category="broad",
        accent="#e5484d",
        funds=(
            FundDef("510300", "沪深300ETF华泰柏瑞"),
            FundDef("510310", "沪深300ETF易方达"),
            FundDef("510320", "沪深300ETF中金"),
            FundDef("510330", "沪深300ETF华夏"),
            FundDef("510350", "沪深300ETF工银"),
        ),
    ),
    GroupDef(
        id="zz500",
        label="中证500 ETF",
        category="broad",
        accent="#3b82f6",
        funds=(
            FundDef("510500", "中证500ETF南方"),
            FundDef("510510", "中证500ETF广发"),
            FundDef("510580", "中证500ETF易方达"),
            FundDef("512500", "中证500ETF华夏"),
            FundDef("512510", "中证500ETF华泰柏瑞"),
        ),
    ),
    GroupDef(
        id="margin",
        label="融资融券",
        category="broad",
        accent="#0ea5a4",
    ),
    GroupDef(
        id="coalPrice",
        label="煤炭价格",
        category="commodity",
        accent="#94a3b8",
        available=False,
    ),
    GroupDef(
        id="aluminum",
        label="电解铝·氧化铝",
        category="commodity",
        accent="#f97316",
        available=False,
    ),
    GroupDef(
        id="aluminumInventory",
        label="铝锭库存",
        category="commodity",
        accent="#f59e0b",
        available=False,
    ),
    GroupDef(
        id="copperPrice",
        label="电解铜价格",
        category="commodity",
        accent="#d97706",
        available=False,
    ),
    GroupDef(
        id="copperInventory",
        label="电解铜库存",
        category="commodity",
        accent="#b45309",
        available=False,
    ),
    GroupDef(
        id="lithiumSpot",
        label="碳酸锂现货",
        category="commodity",
        accent="#10b981",
        available=False,
    ),
    GroupDef(
        id="lithiumWarrant",
        label="碳酸锂仓单",
        category="commodity",
        accent="#059669",
        available=False,
    ),
    GroupDef(
        id="goldReserves",
        label="黄金储备",
        category="commodity",
        accent="#eab308",
        available=False,
    ),
    GroupDef(
        id="scfi",
        label="集运运价 SCFI",
        category="commodity",
        accent="#14b8a6",
        available=False,
    ),
    GroupDef(
        id="hydropower",
        label="长电来水发电",
        category="commodity",
        accent="#0ea5e9",
        available=False,
    ),
    GroupDef(
        id="threeGorges",
        label="三峡水库水位",
        category="commodity",
        accent="#22c55e",
        available=False,
    ),
)
_GROUP_BY_ID = {group.id: group for group in GROUPS}
_KNOWN_CODES = frozenset(fund.code for group in GROUPS for fund in group.funds)


@dataclass(slots=True)
class DayFetch:
    rows: list[dict[str, Any]]
    total: int


def _now_cn() -> datetime:
    return datetime.now(_CN_TZ)


def today_cn() -> date:
    return _now_cn().date()


def catalog_payload() -> dict[str, Any]:
    return {
        "default_group": GROUPS[0].id,
        "default_days": _DEFAULT_DAYS,
        "allowed_days": list(_ALLOWED_DAYS),
        "groups": [
            {
                "id": group.id,
                "label": group.label,
                "category": group.category,
                "available": group.available,
                "accent": group.accent,
                "funds": [{"code": fund.code, "name": fund.name} for fund in group.funds],
            }
            for group in GROUPS
        ],
    }


def target_trade_dates(days: int, end: date | None = None) -> list[str]:
    cursor = end or today_cn()
    found: list[str] = []
    guard = max(days * 3 + 45, 90)
    while len(found) < days and guard > 0:
        if is_trading_day("CN", cursor):
            found.append(cursor.isoformat())
        cursor -= timedelta(days=1)
        guard -= 1
    found.reverse()
    return found


def parse_sse_payload(payload: Any) -> DayFetch | None:
    if not isinstance(payload, dict):
        return None
    raw_rows = payload.get("result")
    if not isinstance(raw_rows, list):
        return None
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("SEC_CODE") or "").strip()
        if code not in _KNOWN_CODES or code in seen:
            continue
        try:
            shares_yi = float(raw.get("TOT_VOL")) / 10_000
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "code": code,
                "name": str(raw.get("SEC_NAME") or "").strip(),
                "shares_yi": round(shares_yi, 6),
            }
        )
        seen.add(code)
    return DayFetch(rows=rows, total=len(raw_rows))


def fetch_sse_day(trade_date: str) -> DayFetch | None:
    params = {
        "isPagination": "true",
        "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
        "sqlId": _SSE_SQL_ID,
        "STAT_DATE": trade_date,
    }
    payload = market_get(
        _SSE_URL,
        host_key="query.sse.com.cn",
        params=params,
        headers=_SSE_HEADERS,
        parse="json",
        timeout=12,
        retries=1,
        total_timeout=30,
        min_interval_s=1.0,
        log_label=f"ETF份额 {trade_date}",
    )
    result = parse_sse_payload(payload)
    if result is None:
        logger.warning("[ETF份额] %s 返回的数据格式不可用", trade_date)
    return result


_write_lock = threading.Lock()
_attempt_lock = threading.Lock()
_last_attempt: dict[str, float] = {}
_backfill_lock = threading.Lock()
_backfill_running = False
_backfill_blocked_until = 0.0


def _claim_attempt(trade_date: str, *, force: bool) -> bool:
    now = time.monotonic()
    with _attempt_lock:
        if not force and now - _last_attempt.get(trade_date, 0.0) < _TODAY_RETRY_S:
            return False
        _last_attempt[trade_date] = now
        return True


def _day_is_covered(db: Session, trade_date: str) -> bool:
    return db.get(EtfShareTradeDay, trade_date) is not None


def _sync_day(
    db: Session,
    trade_date: str,
    *,
    force: bool = False,
) -> bool:
    """抓取单日数据并落库；返回是否拿到本组基金的有效份额。"""
    if not force and _day_is_covered(db, trade_date):
        return False
    if not _claim_attempt(trade_date, force=force):
        return False

    result = fetch_sse_day(trade_date)
    if result is None:
        return False

    today = today_cn().isoformat()
    if result.total == 0 and trade_date == today:
        # 当日清算数据尚未披露，不写入交易日记录，次日会作为历史日期重新补齐。
        return False

    with _write_lock:
        if not force and _day_is_covered(db, trade_date):
            return False
        try:
            existing = {
                row.code: row
                for row in db.query(EtfShareSnapshot)
                .filter(EtfShareSnapshot.trade_date == trade_date)
                .all()
            }
            for item in result.rows:
                snapshot = existing.get(item["code"])
                if snapshot is None:
                    db.add(
                        EtfShareSnapshot(
                            trade_date=trade_date,
                            code=item["code"],
                            name=item["name"],
                            shares_yi=item["shares_yi"],
                        )
                    )
                else:
                    snapshot.name = item["name"]
                    snapshot.shares_yi = item["shares_yi"]
            day_row = db.get(EtfShareTradeDay, trade_date)
            if day_row is None:
                day_row = EtfShareTradeDay(trade_date=trade_date, total=result.total)
                db.add(day_row)
            else:
                day_row.total = result.total
                day_row.fetched_at = datetime.utcnow()
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.info("[ETF份额] %s 已被其他同步任务写入，跳过重复写入", trade_date)
        except Exception:
            db.rollback()
            logger.exception("[ETF份额] %s 数据写入失败", trade_date)
            return False
    return bool(result.rows)


def _group_has_rows(
    db: Session,
    group: GroupDef,
    trade_date: str | None = None,
    window: list[str] | None = None,
) -> bool:
    query = db.query(EtfShareSnapshot.id).filter(
        EtfShareSnapshot.code.in_([fund.code for fund in group.funds])
    )
    if trade_date is not None:
        query = query.filter(EtfShareSnapshot.trade_date == trade_date)
    if window:
        query = query.filter(
            EtfShareSnapshot.trade_date >= window[0],
            EtfShareSnapshot.trade_date <= window[-1],
        )
    return query.first() is not None


def _sync_for_response(db: Session, group: GroupDef, targets: list[str], *, force: bool) -> None:
    if not targets:
        return
    today = today_cn().isoformat()
    latest = targets[-1]
    prior = [day for day in targets if day < today]

    if force:
        got_rows = _sync_day(db, latest, force=True)
        if latest == today and not got_rows and prior:
            _sync_day(db, prior[-1], force=True)
        return

    if not _group_has_rows(db, group, window=targets):
        for day in reversed(targets[-5:]):
            if _sync_day(db, day) and _group_has_rows(db, group, day):
                break
        return

    if latest == today and _now_cn().hour < _PUBLISH_HOUR:
        if prior and not _day_is_covered(db, prior[-1]):
            _sync_day(db, prior[-1])
        return

    if not _day_is_covered(db, latest):
        _sync_day(db, latest)


def _backfill_worker(days: tuple[str, ...]) -> None:
    global _backfill_running, _backfill_blocked_until
    db = SessionLocal()
    failures = 0
    try:
        for day in days:
            if _day_is_covered(db, day):
                continue
            result = fetch_sse_day(day)
            if result is None:
                failures += 1
                if failures >= 3:
                    logger.warning("[ETF份额] 历史补齐连续失败，暂停本轮任务")
                    break
                continue
            failures = 0
            _sync_day(db, day)
    except Exception:
        logger.exception("[ETF份额] 历史补齐任务异常结束")
    finally:
        db.close()
        with _backfill_lock:
            _backfill_running = False
            if failures:
                _backfill_blocked_until = time.monotonic() + _BACKFILL_RETRY_S


def _start_backfill(days: list[str]) -> None:
    global _backfill_running
    if not days:
        return
    with _backfill_lock:
        if _backfill_running or time.monotonic() < _backfill_blocked_until:
            return
        _backfill_running = True
    thread = threading.Thread(
        target=_backfill_worker,
        args=(tuple(reversed(days)),),
        name="etf-share-backfill",
        daemon=True,
    )
    thread.start()


def _require_group(group_id: str) -> GroupDef:
    group = _GROUP_BY_ID.get(group_id)
    if group is None:
        raise ValueError(f"unknown ETF share group: {group_id}")
    return group


def build_curve_payload(
    db: Session,
    group_id: str,
    days: int = _DEFAULT_DAYS,
    *,
    targets: list[str] | None = None,
) -> dict[str, Any]:
    group = _require_group(group_id)
    window = targets if targets is not None else target_trade_dates(days)
    codes = [fund.code for fund in group.funds]
    if not window:
        return {
            "group": group.id,
            "group_label": group.label,
            "as_of": None,
            "dates": [],
            "funds": [],
            "coverage": {"trading_days": 0, "target": 0, "complete": False},
        }

    snapshots = (
        db.query(EtfShareSnapshot)
        .filter(
            EtfShareSnapshot.code.in_(codes),
            EtfShareSnapshot.trade_date >= window[0],
            EtfShareSnapshot.trade_date <= window[-1],
        )
        .all()
    )
    values_by_code: dict[str, dict[str, float]] = {code: {} for code in codes}
    for snapshot in snapshots:
        values_by_code.setdefault(snapshot.code, {})[snapshot.trade_date] = snapshot.shares_yi

    dates = [
        day
        for day in window
        if any(values_by_code.get(code, {}).get(day) is not None for code in codes)
    ]

    funds: list[dict[str, Any]] = []
    for fund in group.funds:
        series = values_by_code.get(fund.code, {})
        present = [(day, series[day]) for day in dates if series.get(day) is not None]
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
        funds.append(
            {
                "code": fund.code,
                "name": fund.name,
                "latest": round(latest, 6),
                "latest_date": latest_date,
                "delta": delta,
                "delta_pct": delta_pct,
                "values": [
                    round(series[day], 6) if series.get(day) is not None else None
                    for day in dates
                ],
            }
        )

    today = today_cn().isoformat()
    covered = {
        row.trade_date
        for row in db.query(EtfShareTradeDay.trade_date)
        .filter(EtfShareTradeDay.trade_date.in_(window))
        .all()
    }
    required = [day for day in window if day != today]
    complete = all(day in covered for day in required) if required else bool(window)

    return {
        "group": group.id,
        "group_label": group.label,
        "as_of": max((fund["latest_date"] for fund in funds), default=None),
        "dates": dates,
        "funds": funds,
        "coverage": {
            "trading_days": len(dates),
            "target": len(window),
            "complete": complete,
        },
    }


def get_curve_payload(
    db: Session,
    group_id: str,
    days: int = _DEFAULT_DAYS,
    *,
    force: bool = False,
    network: bool = True,
) -> dict[str, Any]:
    group = _require_group(group_id)
    targets = target_trade_dates(days)
    if network:
        _sync_for_response(db, group, targets, force=force)
        today = today_cn().isoformat()
        missing = [
            day
            for day in targets
            if day < today and not _day_is_covered(db, day)
        ]
        _start_backfill(missing)
    return build_curve_payload(db, group_id, days, targets=targets)
