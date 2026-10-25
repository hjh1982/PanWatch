"""A 股恐贪指数：百分位（baifenwei.com）0–100 情绪读数。

来源为公开静态 JSON，页面约每小时构建一次。指数由波动率、相对换手率、
融资融券、市场宽度、RSI、涨跌停比六项子指标加权得到，越高越偏贪婪。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from src.platform.marketdata.collectors.market_http import market_get
from src.platform.persistence.models import FearGreedPoint

logger = logging.getLogger(__name__)

_SOURCE_URL = "https://baifenwei.com/data/fear-greed/series-1.json"
_SOURCE_PAGE = "https://baifenwei.com/indicator/fear-greed/"
_SOURCE_NAME = "百分位 baifenwei.com"
_SOURCE_METHOD = "波动率、相对换手率、融资融券、市场宽度、RSI、涨跌停比六项子指标加权，0–100 分"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": _SOURCE_PAGE,
    "Accept": "application/json, text/plain, */*",
}
_SYNC_INTERVAL_S = 30 * 60
_DEFAULT_DAYS = 30

_state = {
    "last_attempt": 0.0,
    "generated_at": "",
    "lock": threading.Lock(),
}


class FearGreedUnavailable(RuntimeError):
    """数据源与本地缓存均无可用读数。"""


@dataclass(frozen=True, slots=True)
class FearGreedRow:
    trade_date: str
    score: float
    shenzhen_index: float | None


def level_for(score: float) -> str:
    if score <= 25:
        return "extreme_fear"
    if score <= 45:
        return "fear"
    if score <= 55:
        return "neutral"
    if score <= 75:
        return "greed"
    return "extreme_greed"


def parse_payload(payload: dict[str, Any]) -> tuple[list[FearGreedRow], str]:
    """把 ``points: [[date, score, index], ...]`` 解析为按日期升序的有效读数。"""
    raw_points = payload.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise ValueError("points missing")

    rows: list[FearGreedRow] = []
    for item in raw_points:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        day = str(item[0] or "").strip()
        if len(day) != 10 or day[4] != "-" or day[7] != "-":
            continue
        try:
            score = float(item[1])
        except (TypeError, ValueError):
            continue
        if not 0 <= score <= 100:
            continue
        index_value: float | None = None
        if len(item) > 2 and item[2] is not None:
            try:
                index_value = float(item[2])
            except (TypeError, ValueError):
                index_value = None
        rows.append(FearGreedRow(day, score, index_value))

    if not rows:
        raise ValueError("no valid points")
    rows.sort(key=lambda row: row.trade_date)
    generated_at = str(payload.get("generated_at") or "").strip()
    return rows, generated_at


def _sync_from_source(db: Session) -> None:
    payload = market_get(
        _SOURCE_URL,
        host_key="baifenwei.com",
        headers=_HEADERS,
        parse="json",
        min_interval_s=1.0,
        timeout=15.0,
        retries=1,
        total_timeout=25.0,
        log_label="百分位恐贪指数",
    )
    if not isinstance(payload, dict):
        raise FearGreedUnavailable("数据源无响应")
    rows, generated_at = parse_payload(payload)
    if not rows:
        raise FearGreedUnavailable("数据源无有效读数")

    statement = sqlite_insert(FearGreedPoint).values(
        [
            {
                "trade_date": row.trade_date,
                "score": row.score,
                "shenzhen_index": row.shenzhen_index,
            }
            for row in rows
        ]
    )
    statement = statement.on_conflict_do_update(
        index_elements=[FearGreedPoint.trade_date],
        set_={
            "score": statement.excluded.score,
            "shenzhen_index": statement.excluded.shenzhen_index,
        },
    )
    db.execute(statement)
    db.commit()
    _state["generated_at"] = generated_at


def _maybe_sync(db: Session, *, force: bool = False) -> None:
    now = time.monotonic()
    if not force and now - _state["last_attempt"] < _SYNC_INTERVAL_S:
        return
    with _state["lock"]:
        now = time.monotonic()
        if not force and now - _state["last_attempt"] < _SYNC_INTERVAL_S:
            return
        _state["last_attempt"] = now
        try:
            _sync_from_source(db)
        except Exception as exc:
            db.rollback()
            logger.warning("A股恐贪指数同步失败，使用本地缓存: %s", exc)


def build_payload(
    rows: Sequence[Any],
    days: int = _DEFAULT_DAYS,
    generated_at: str | None = None,
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: str(row.trade_date))
    selected = ordered[-max(2, days):]
    latest = selected[-1]
    previous = selected[-2] if len(selected) >= 2 else None
    score = round(float(latest.score), 2)
    return {
        "date": str(latest.trade_date),
        "score": score,
        "level": level_for(score),
        "change": (
            round(score - round(float(previous.score), 2), 2)
            if previous is not None
            else None
        ),
        "shenzhen_index": latest.shenzhen_index,
        "history": [
            {
                "date": str(row.trade_date),
                "score": round(float(row.score), 2),
            }
            for row in selected
        ],
        "source": {
            "name": _SOURCE_NAME,
            "url": _SOURCE_PAGE,
            "methodology": _SOURCE_METHOD,
            "generated_at": generated_at or None,
        },
    }


def get_payload(db: Session, days: int = _DEFAULT_DAYS, *, force: bool = False) -> dict[str, Any]:
    _maybe_sync(db, force=force)
    rows = (
        db.query(FearGreedPoint)
        .order_by(FearGreedPoint.trade_date.asc())
        .all()
    )
    if not rows:
        raise FearGreedUnavailable("暂无恐贪指数数据")
    return build_payload(rows, days, _state["generated_at"] or None)
