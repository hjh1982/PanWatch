"""动力煤月度长协价：CCTD 秦皇岛动力煤长协价（5500/5000/4500 大卡，每月 1 日发布）。"""

from __future__ import annotations

import csv
import json
import logging
import math
import re
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.modules.market.etf_share_service import _require_group, today_cn
from src.platform.persistence.database import DB_PATH
from src.platform.persistence.models import CoalLtcPoint

logger = logging.getLogger(__name__)

COAL_LTC_GROUP_ID = "coalLtc"
_CSV = Path(__file__).with_name("resources") / "coal_ltc_monthly.csv"
_PAGE_URL = (
    "https://www.cctd.com.cn/index.php?m=content&c=index&a=lists&catid=454"
    "&data=CCTD%C7%D8%BB%CA%B5%BA%B6%AF%C1%A6%C3%BA%BC%DB%B8%F1"
    "&name=CCTD%C7%D8%BB%CA%B5%BA%B6%AF%C1%A6%C3%BA%BC%DB%B8%F1"
)
_DATASET = "CCTD秦皇岛长协价格"
# 站点自带的「不限时间」过滤条件，等价于取全部历史。
_ALL_HISTORY = " where  DATE_FORMAT(END_DATE,'%Y-%m-%d') >= '-0002-11-30'"
_DATA_URL = "https://www.cctd.com.cn/datasql.php"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": _PAGE_URL,
}

# (指标 id, 数据字段, 表列名)
_METRICS = (
    ("ltc_5500", "price_5500", "price_5500"),
    ("ltc_5000", "price_5000", "price_5000"),
    ("ltc_4500", "price_4500", "price_4500"),
)
_MONTH_RE = re.compile(r"^20\d{2}-(0[1-9]|1[0-2])$")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "—"):
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_seed_rows(path: Path = _CSV) -> list[dict[str, Any]]:
    """读取内置长协价种子数据，单位元/吨。"""
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            raw = list(csv.DictReader(handle))
    except OSError:
        logger.warning("[动力煤长协价] 种子文件不可读：%s", path)
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        month = (item.get("month") or "").strip()[:7]
        price_5500 = _to_float(item.get("price_5500"))
        if not _MONTH_RE.match(month) or price_5500 is None or price_5500 <= 0:
            continue
        rows.append(
            {
                "month": month,
                "price_5500": price_5500,
                "price_5000": _to_float(item.get("price_5000")),
                "price_4500": _to_float(item.get("price_4500")),
            }
        )
    if not rows:
        logger.warning("[动力煤长协价] 种子文件为空：%s", path)
    return rows


def _ensure_seeded(db: Session) -> None:
    if db.query(CoalLtcPoint.id).first() is not None:
        return
    rows = load_seed_rows()
    if not rows:
        return
    db.add_all(CoalLtcPoint(**row) for row in rows)
    try:
        db.commit()
    except IntegrityError:
        # 并发请求可能已经完成初始化，保留已有数据即可。
        db.rollback()


def load_points(db: Session) -> list[dict[str, Any]]:
    _ensure_seeded(db)
    records = (
        db.query(
            CoalLtcPoint.month,
            CoalLtcPoint.price_5500,
            CoalLtcPoint.price_5000,
            CoalLtcPoint.price_4500,
        )
        .order_by(CoalLtcPoint.month)
        .all()
    )
    return [
        {
            "month": str(month),
            "price_5500": float(price_5500),
            "price_5000": float(price_5000) if price_5000 is not None else None,
            "price_4500": float(price_4500) if price_4500 is not None else None,
        }
        for month, price_5500, price_5000, price_4500 in records
    ]


def _data_url() -> str:
    # 站点以 GBK 传参，需按 GBK 百分号编码，否则查不到数据集。
    encoded = quote(_DATASET.encode("gbk"))
    return (
        f"{_DATA_URL}?data={encoded}&name={encoded}"
        f"&time={quote(_ALL_HISTORY.encode('gbk'))}"
    )


def parse_data_rows(payload: Any) -> list[dict[str, Any]]:
    """解析 datatables 响应：[月份, 5500价, 5500环比, 5000价, 5000环比, 4500价, 4500环比]。"""
    if not isinstance(payload, dict):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        if not isinstance(item, (list, tuple)) or len(item) < 5:
            continue
        month = str(item[0]).strip()[:7]
        price_5500 = _to_float(item[1])
        if not _MONTH_RE.match(month) or price_5500 is None or price_5500 <= 0:
            continue
        rows.append(
            {
                "month": month,
                "price_5500": price_5500,
                "price_5000": _to_float(item[3]),
                "price_4500": _to_float(item[5]) if len(item) > 5 else None,
            }
        )
    return rows


def fetch_cctd_rows() -> list[dict[str, Any]]:
    with httpx.Client(timeout=25, follow_redirects=True, headers=_HEADERS) as client:
        response = client.get(_data_url())
        response.raise_for_status()
        return parse_data_rows(response.json())


def sync_from_cctd(db: Session) -> int:
    """按月份回填长协价；接口不可用时保留本地数据。"""
    try:
        rows = fetch_cctd_rows()
    except Exception as exc:
        logger.warning("[动力煤长协价] 同步 CCTD 失败，沿用本地数据：%s", exc)
        return 0
    if not rows:
        return 0
    existing = {record.month: record for record in db.query(CoalLtcPoint).all()}
    changed = 0
    for row in rows:
        record = existing.get(row["month"])
        if record is None:
            db.add(CoalLtcPoint(**row))
            changed += 1
            continue
        for field in ("price_5500", "price_5000", "price_4500"):
            value = row.get(field)
            if value is not None and getattr(record, field) != value:
                setattr(record, field, value)
                changed += 1
    if not changed:
        return 0
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.warning("[动力煤长协价] 同步写入冲突，沿用本地数据")
        return 0
    return changed


def _metric(
    metric_id: str, months: list[str], values: list[float | None]
) -> dict[str, Any] | None:
    indexes = [
        index
        for index, value in enumerate(values)
        if isinstance(value, (int, float)) and math.isfinite(value)
    ]
    if not indexes:
        return None
    latest_index = indexes[-1]
    latest = float(values[latest_index])
    previous = float(values[indexes[-2]]) if len(indexes) > 1 else None
    return {
        "id": metric_id,
        "latest": round(latest, 2),
        "latest_date": months[latest_index],
        "delta": round(latest - previous, 2) if previous is not None else None,
        "delta_pct": (
            round((latest - previous) / previous * 100, 4)
            if previous not in (None, 0)
            else None
        ),
    }


def _slice_months(months: list[str], days: int, *, full: bool) -> int:
    if full or not months:
        return 0
    cutoff = (today_cn() - timedelta(days=days)).isoformat()[:7]
    return next(
        (index for index, month in enumerate(months) if month >= cutoff),
        len(months) - 1,
    )


def _storage_payload() -> dict[str, str]:
    return {
        "database": str(Path(DB_PATH).resolve()),
        "table": CoalLtcPoint.__tablename__,
        "seed": str(_CSV.resolve()),
    }


def build_payload(
    points: list[dict[str, Any]],
    days: int,
    *,
    full: bool = False,
) -> dict[str, Any]:
    group = _require_group(COAL_LTC_GROUP_ID)
    months = [point["month"] for point in points]
    start = _slice_months(months, days, full=full)
    window = months[start:]
    series = {
        metric_id: [point[field] for point in points[start:]]
        for metric_id, field, _ in _METRICS
    }
    metrics = [
        metric
        for metric in (
            _metric(metric_id, window, series[metric_id])
            for metric_id, _, _ in _METRICS
        )
        if metric is not None
    ]
    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "秦皇岛 5500/5000/4500 大卡",
        "as_of": window[-1] if window else None,
        "dates": window,
        "metrics": metrics,
        "series": series,
        "coverage": {
            "trading_days": len(window),
            "target": len(window),
            "complete": bool(window),
        },
        "storage": _storage_payload(),
        "source": {
            "name": "CCTD 中国煤炭市场网",
            "url": _PAGE_URL,
            "cadence": "月度",
        },
    }


def get_curve_payload(
    db: Session,
    group_id: str,
    days: int,
    *,
    force: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    _require_group(group_id)
    _ensure_seeded(db)
    if force:
        sync_from_cctd(db)
    return build_payload(load_points(db), days, full=full)
