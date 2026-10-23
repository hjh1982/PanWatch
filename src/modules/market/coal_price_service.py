"""煤炭价格：CCI5500 动力煤现货价（日度）。"""

from __future__ import annotations

import csv
import logging
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.modules.market.etf_share_service import _require_group, today_cn
from src.platform.persistence.database import DB_PATH
from src.platform.persistence.models import CoalPricePoint

logger = logging.getLogger(__name__)

COAL_GROUP_ID = "coalPrice"
# 首次读取时用于初始化数据库；来源：行业数据.xlsx「煤炭CCI」表。
_CCI_CSV = Path(__file__).with_name("resources") / "coal_cci_5500.csv"
_METRIC_ID = "cci_5500"


class InvalidCoalDate(ValueError):
    pass


class InvalidCoalPrice(ValueError):
    pass


class CoalPriceDateExists(ValueError):
    pass


class CoalPriceNotFound(ValueError):
    pass


def _to_float(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _select_dates(dates: list[str], days: int) -> list[str]:
    if not dates:
        return []
    cutoff = (today_cn() - timedelta(days=days)).isoformat()
    selected = [day for day in dates if day >= cutoff]
    return selected or dates[-1:]


def _is_canonical_day(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def load_cci_rows(path: Path = _CCI_CSV) -> dict[str, float]:
    """读取内置 CCI5500 日度种子数据，日期格式 YYYY-MM-DD，单位元/吨。"""
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = {
                str(item.get("date") or "")[:10]: _to_float(item.get("cci_5500"))
                for item in csv.DictReader(handle)
            }
    except OSError:
        logger.warning("[煤炭价格] CCI 数据文件不可读：%s", path)
        return {}
    cleaned = {
        day: value
        for day, value in rows.items()
        if _is_canonical_day(day) and value is not None
    }
    if not cleaned:
        logger.warning("[煤炭价格] CCI 数据文件为空：%s", path)
    return cleaned


def _ensure_seeded(db: Session) -> None:
    if db.query(CoalPricePoint.id).first() is not None:
        return
    rows = load_cci_rows()
    if not rows:
        return
    db.add_all(
        CoalPricePoint(trade_date=day, price=price)
        for day, price in sorted(rows.items())
    )
    try:
        db.commit()
    except IntegrityError:
        # 并发请求可能已经完成初始化，保留已有数据即可。
        db.rollback()


def load_price_rows(db: Session) -> dict[str, float]:
    _ensure_seeded(db)
    records = (
        db.query(CoalPricePoint.trade_date, CoalPricePoint.price)
        .order_by(CoalPricePoint.trade_date)
        .all()
    )
    return {str(day): float(price) for day, price in records}


def _normalize_day(raw: str) -> str:
    day = (raw or "").strip()
    try:
        parsed = date.fromisoformat(day)
    except ValueError as exc:
        raise InvalidCoalDate from exc
    return parsed.isoformat()


def _normalize_price(value: float) -> float:
    if not math.isfinite(value) or value <= 0 or not float(value).is_integer():
        raise InvalidCoalPrice
    return float(int(value))


def _point_payload(record: CoalPricePoint) -> dict[str, Any]:
    return {"date": record.trade_date, "price": record.price}


def create_point(db: Session, raw_day: str, price: float) -> dict[str, Any]:
    day = _normalize_day(raw_day)
    value = _normalize_price(price)
    _ensure_seeded(db)
    exists = (
        db.query(CoalPricePoint.id)
        .filter(CoalPricePoint.trade_date == day)
        .first()
    )
    if exists is not None:
        raise CoalPriceDateExists
    record = CoalPricePoint(trade_date=day, price=value)
    db.add(record)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CoalPriceDateExists from exc
    db.refresh(record)
    return _point_payload(record)


def update_point(
    db: Session,
    original_day: str,
    raw_day: str,
    price: float,
) -> dict[str, Any]:
    original = _normalize_day(original_day)
    day = _normalize_day(raw_day)
    value = _normalize_price(price)
    _ensure_seeded(db)
    record = (
        db.query(CoalPricePoint)
        .filter(CoalPricePoint.trade_date == original)
        .first()
    )
    if record is None:
        raise CoalPriceNotFound
    if day != original:
        exists = (
            db.query(CoalPricePoint.id)
            .filter(CoalPricePoint.trade_date == day)
            .first()
        )
        if exists is not None:
            raise CoalPriceDateExists
    record.trade_date = day
    record.price = value
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CoalPriceDateExists from exc
    db.refresh(record)
    return _point_payload(record)


def _storage_payload() -> dict[str, str]:
    return {
        "database": str(Path(DB_PATH).resolve()),
        "table": CoalPricePoint.__tablename__,
        "seed": str(_CCI_CSV.resolve()),
    }


def build_price_payload(
    rows: dict[str, float],
    days: int,
    *,
    full: bool = False,
) -> dict[str, Any]:
    group = _require_group(COAL_GROUP_ID)
    ordered = sorted(rows.items())
    if full:
        window = ordered
    else:
        selected = set(_select_dates([day for day, _ in ordered], days))
        window = [(day, value) for day, value in ordered if day in selected]
    dates = [day for day, _ in window]
    values = [round(value, 6) for _, value in window]

    metrics: list[dict[str, Any]] = []
    if values:
        latest = values[-1]
        previous = values[-2] if len(values) > 1 else None
        metrics.append(
            {
                "id": _METRIC_ID,
                "latest": latest,
                "latest_date": dates[-1],
                "delta": round(latest - previous, 6) if previous is not None else None,
                "delta_pct": (
                    round((latest - previous) / previous * 100, 4)
                    if previous not in (None, 0)
                    else None
                ),
            }
        )

    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "CCI5500",
        "as_of": dates[-1] if dates else None,
        "dates": dates,
        "metrics": metrics,
        "series": {_METRIC_ID: values},
        "coverage": {
            "trading_days": len(dates),
            "target": len(dates),
            "complete": bool(dates),
        },
        "storage": _storage_payload(),
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
    return build_price_payload(load_price_rows(db), days, full=full)
