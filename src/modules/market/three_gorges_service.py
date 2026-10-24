"""三峡水库水位：湖北水文三峡站逐小时库水位、蓄水量与出入库流量。"""

from __future__ import annotations

import html as html_lib
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any

from sqlalchemy.orm import Session

from src.modules.market.etf_share_service import _require_group, today_cn
from src.platform.marketdata.collectors.market_http import market_get
from src.platform.persistence.models import ThreeGorgesWaterPoint

logger = logging.getLogger(__name__)

THREE_GORGES_GROUP_ID = "threeGorges"
_STATION_CODE = "60106980"
# 该站 TLS 只支持 legacy renegotiation，OpenSSL 3 默认拒绝握手；页面同时提供明文 HTTP。
_URL = "http://hbwater.wetruetech.com/water/portal/wx_station_info"
_WINDOW_DAYS = 30
_HISTORY_MAX_WINDOWS = 18
_RECENT_SYNC_INTERVAL_S = 15 * 60
_HISTORY_SYNC_INTERVAL_S = 6 * 3600
_EMPTY_RETRY_S = 60
_SOURCE_RETENTION_DAYS = 500
_BATCH_HOUR = 8
_STORAGE_DIVISOR = 100.0  # 10^6 m^3 -> 亿 m^3

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "http://hbwater.wetruetech.com/water/portal/lake_details2",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_ROW = re.compile(r"<tr[^>]*>([\s\S]*?)</tr>", re.I)
_CELL = re.compile(r"<td[^>]*>([\s\S]*?)</td>", re.I)
_TAG = re.compile(r"<[^>]+>")
_OBS_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2})(?::\d{2})?$")

_state = {
    # monotonic 从开机计时，0.0 会被误判为“刚同步过”；用 -inf 表示从未同步。
    "recent_at": -math.inf,
    "history_at": -math.inf,
    "lock": threading.Lock(),
}


@dataclass(frozen=True, slots=True)
class Observation:
    observed_at: str
    day: str
    hour: int
    level_m: float | None
    storage_yi_m3: float | None
    inflow_m3s: float | None
    outflow_m3s: float | None


@dataclass(frozen=True, slots=True)
class DailyPoint:
    day: str
    observed_at: str
    level_m: float
    storage_yi_m3: float | None
    inflow_m3s: float | None
    outflow_m3s: float | None


def _to_float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _clean(fragment: str) -> str:
    return html_lib.unescape(_TAG.sub("", fragment)).strip()


def parse_station_rows(page_html: str) -> list[Observation]:
    """解析三峡站逐小时统计表，忽略表头与异常行。"""
    observations: list[Observation] = []
    for row in _ROW.findall(page_html or ""):
        cells = [_clean(cell) for cell in _CELL.findall(row)]
        if len(cells) < 2:
            continue
        match = _OBS_TIME.match(cells[0])
        if not match:
            continue
        year, month, day, hour = (int(part) for part in match.groups())
        try:
            moment = datetime(year, month, day, hour)
        except ValueError:
            continue
        level = _to_float(cells[1])
        if level is None or not 80 <= level <= 200:
            continue
        storage = _to_float(cells[2]) if len(cells) > 2 else None
        inflow = _to_float(cells[3]) if len(cells) > 3 else None
        outflow = _to_float(cells[4]) if len(cells) > 4 else None
        observations.append(
            Observation(
                observed_at=moment.strftime("%Y-%m-%d %H"),
                day=moment.date().isoformat(),
                hour=hour,
                level_m=level,
                storage_yi_m3=round(storage / _STORAGE_DIVISOR, 2)
                if storage is not None
                else None,
                inflow_m3s=inflow,
                outflow_m3s=outflow,
            )
        )
    return observations


def pick_daily_points(
    observations: list[Observation],
) -> dict[str, DailyPoint]:
    """每个自然日取 8 时观测；8 时缺失时退回当日最后一条。"""
    grouped: dict[str, list[Observation]] = {}
    for observation in observations:
        grouped.setdefault(observation.day, []).append(observation)

    points: dict[str, DailyPoint] = {}
    for day, items in grouped.items():
        batch = next((item for item in items if item.hour == _BATCH_HOUR), None)
        latest = max(items, key=lambda item: item.hour)
        chosen = batch or latest
        level = chosen.level_m
        if level is None:
            continue
        points[day] = DailyPoint(
            day=day,
            observed_at=chosen.observed_at,
            level_m=level,
            storage_yi_m3=chosen.storage_yi_m3
            if chosen.storage_yi_m3 is not None
            else latest.storage_yi_m3,
            inflow_m3s=chosen.inflow_m3s
            if chosen.inflow_m3s is not None
            else latest.inflow_m3s,
            outflow_m3s=chosen.outflow_m3s
            if chosen.outflow_m3s is not None
            else latest.outflow_m3s,
        )
    return points


def _fetch_range(
    start: date,
    end: date,
    *,
    max_windows: int,
) -> dict[str, DailyPoint]:
    """从后向前翻页抓取区间；源站单次最多返回约 30 天逐小时数据。"""
    floor = datetime.combine(start, dt_time.min)
    cursor = datetime.combine(end, dt_time(23))
    observations: dict[str, Observation] = {}
    for _ in range(max_windows):
        page_html = market_get(
            _URL,
            host_key="hbwater.wetruetech.com",
            params={
                "startTm": floor.strftime("%Y-%m-%d %H"),
                "endTm": cursor.strftime("%Y-%m-%d %H"),
                "stationCode": _STATION_CODE,
                "stationType": "",
            },
            headers=_HEADERS,
            min_interval_s=1.0,
            timeout=25.0,
            total_timeout=60.0,
            retries=1,
            log_label="three-gorges-water",
        )
        if not page_html:
            break
        rows = parse_station_rows(page_html)
        if not rows:
            break
        for row in rows:
            observations[row.observed_at] = row
        earliest = min(datetime.strptime(row.observed_at, "%Y-%m-%d %H") for row in rows)
        if earliest <= floor:
            break
        next_cursor = earliest - timedelta(hours=1)
        if next_cursor >= cursor:
            break
        cursor = next_cursor
    return pick_daily_points(list(observations.values()))


def _store_points(db: Session, points: dict[str, DailyPoint]) -> None:
    if not points:
        return
    first_day = min(points)
    records = {
        str(record.report_date): record
        for record in db.query(ThreeGorgesWaterPoint)
        .filter(ThreeGorgesWaterPoint.report_date >= first_day)
        .all()
    }
    for day, point in points.items():
        record = records.get(day)
        if record is None:
            record = ThreeGorgesWaterPoint(report_date=day)
            db.add(record)
            records[day] = record
        if point.observed_at >= (record.observed_at or ""):
            record.observed_at = point.observed_at
        if point.level_m is not None:
            record.level_m = point.level_m
        if point.storage_yi_m3 is not None:
            record.storage_yi_m3 = point.storage_yi_m3
        if point.inflow_m3s is not None:
            record.inflow_m3s = point.inflow_m3s
        if point.outflow_m3s is not None:
            record.outflow_m3s = point.outflow_m3s
    db.commit()


def _sync_rows(db: Session, start: date, end: date, *, force: bool) -> None:
    now = time.monotonic()
    recent_due = force or now - _state["recent_at"] >= _RECENT_SYNC_INTERVAL_S
    history_due = force or now - _state["history_at"] >= _HISTORY_SYNC_INTERVAL_S
    if not recent_due and not history_due:
        return

    points: dict[str, DailyPoint] = {}
    if recent_due:
        recent_start = max(start, end - timedelta(days=_WINDOW_DAYS - 1))
        recent_points = _fetch_range(recent_start, end, max_windows=1)
        points.update(recent_points)
        _state["recent_at"] = _next_sync_at(
            now, bool(recent_points), _RECENT_SYNC_INTERVAL_S
        )
    if history_due:
        history_end = end - timedelta(days=_WINDOW_DAYS)
        history_points: dict[str, DailyPoint] = {}
        if history_end >= start:
            span_days = (history_end - start).days + 1
            windows = min(
                _HISTORY_MAX_WINDOWS,
                max(1, math.ceil(span_days / _WINDOW_DAYS)),
            )
            history_points = _fetch_range(start, history_end, max_windows=windows)
            points.update(history_points)
        _state["history_at"] = _next_sync_at(
            now,
            bool(history_points) or history_end < start,
            _HISTORY_SYNC_INTERVAL_S,
        )

    _store_points(db, points)


def _next_sync_at(now: float, fetched: bool, interval_s: int) -> float:
    """成功取到数据才按正常周期节流；空页仅短暂退避，避免长期只拿到部分区间。"""
    if fetched:
        return now
    return now - interval_s + _EMPTY_RETRY_S


def _needs_sync(db: Session) -> bool:
    now = time.monotonic()
    return (
        now - _state["recent_at"] >= _RECENT_SYNC_INTERVAL_S
        or now - _state["history_at"] >= _HISTORY_SYNC_INTERVAL_S
    )


def load_rows(db: Session) -> dict[str, dict[str, float | str | None]]:
    rows: dict[str, dict[str, float | str | None]] = {}
    for record in db.query(ThreeGorgesWaterPoint).order_by(
        ThreeGorgesWaterPoint.report_date
    ):
        rows[str(record.report_date)] = {
            "observed_at": record.observed_at,
            "level_m": record.level_m,
            "storage_yi_m3": record.storage_yi_m3,
            "inflow_m3s": record.inflow_m3s,
            "outflow_m3s": record.outflow_m3s,
        }
    return rows


def _previous_day(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


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
    previous = present[-2] if len(present) > 1 else None
    adjacent = previous is not None and previous[0] == _previous_day(latest_date)
    delta = round(latest - previous[1], 6) if adjacent else None
    delta_pct = (
        round((latest - previous[1]) / previous[1] * 100, 4)
        if adjacent and previous[1] not in (None, 0)
        else None
    )
    return {
        "id": metric_id,
        "latest": round(float(latest), 6),
        "latest_date": latest_date,
        "delta": delta,
        "delta_pct": delta_pct,
    }


def build_payload(
    rows: dict[str, dict[str, float | str | None]],
    days: int,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    group = _require_group(THREE_GORGES_GROUP_ID)
    today = today or today_cn()
    all_dates = sorted(rows)
    start = today - timedelta(days=max(days, 1) - 1)
    window_start = max(start, date.fromisoformat(all_dates[0])) if all_dates else start
    dates = [
        (window_start + timedelta(days=offset)).isoformat()
        for offset in range((today - window_start).days + 1)
    ]

    level = [rows.get(day, {}).get("level_m") for day in dates]
    inflow = [rows.get(day, {}).get("inflow_m3s") for day in dates]
    outflow = [rows.get(day, {}).get("outflow_m3s") for day in dates]

    metrics = [
        metric
        for metric in (
            _metric("level", dates, level),
            _metric("inflow", dates, inflow),
            _metric("outflow", dates, outflow),
        )
        if metric is not None
    ]
    observed = [
        str(rows[day]["observed_at"])
        for day in dates
        if rows.get(day, {}).get("observed_at")
    ]
    present_days = {
        day for day in dates if isinstance(rows.get(day, {}).get("level_m"), (int, float))
    }
    required = [day for day in dates if day != today.isoformat()]
    complete = all(day in present_days for day in required) if required else bool(dates)

    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "湖北水文 · 三峡站 60106980",
        "as_of": observed[-1] if observed else None,
        "dates": dates,
        "metrics": metrics,
        "series": {
            "level": level,
            "inflow": inflow,
            "outflow": outflow,
        },
        "coverage": {
            "trading_days": len(present_days),
            "target": len(dates),
            "complete": complete,
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
    today = today_cn()
    start = today - timedelta(days=min(max(days, 1), _SOURCE_RETENTION_DAYS) - 1)
    with _state["lock"]:
        if force or _needs_sync(db):
            _sync_rows(db, start, today, force=force)
    return build_payload(load_rows(db), days)
