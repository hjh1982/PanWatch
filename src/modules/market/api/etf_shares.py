"""ETF 份额、融资融券、煤炭与电解铝价格 API：目录、曲线与手动刷新。"""

import asyncio
import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.modules.market import (
    aluminum_inventory_service,
    aluminum_service,
    coal_monthly_service,
    coal_price_service,
    etf_share_service,
    margin_service,
    three_gorges_service,
)
from src.modules.market.etf_share_service import catalog_payload
from src.platform.persistence.database import get_db
from src.web.errors import api_error

logger = logging.getLogger(__name__)
router = APIRouter()


class CoalPricePointCommand(BaseModel):
    date: str
    price: float


def _load_curve(
    db: Session,
    group: str,
    days: int,
    *,
    force: bool,
    full: bool,
):
    if group == margin_service.MARGIN_GROUP_ID:
        return margin_service.get_curve_payload(group, days, force=force)
    if group == coal_price_service.COAL_GROUP_ID:
        return coal_price_service.get_curve_payload(
            db, group, days, force=force, full=full
        )
    if group in (
        coal_monthly_service.COAL_MONTHLY_GROUP_ID,
        coal_monthly_service.COAL_POWER_GROUP_ID,
    ):
        return coal_monthly_service.get_curve_payload(
            db, group, days, force=force, full=full
        )
    if group == aluminum_service.ALUMINUM_GROUP_ID:
        return aluminum_service.get_curve_payload(db, group, days, force=force)
    if group == aluminum_inventory_service.ALUMINUM_INVENTORY_GROUP_ID:
        return aluminum_inventory_service.get_curve_payload(
            db, group, days, force=force, full=full
        )
    if group == three_gorges_service.THREE_GORGES_GROUP_ID:
        return three_gorges_service.get_curve_payload(
            db, group, days, force=force
        )
    return etf_share_service.get_curve_payload(db, group, days, force=force)


def _coal_point_error(exc: ValueError):
    if isinstance(exc, coal_price_service.InvalidCoalDate):
        return api_error(400, "coal_price_date_invalid", "日期格式应为 YYYY-MM-DD")
    if isinstance(exc, coal_price_service.InvalidCoalPrice):
        return api_error(400, "coal_price_value_invalid", "价格需为大于 0 的整数")
    if isinstance(exc, coal_price_service.CoalPriceDateExists):
        return api_error(409, "coal_price_date_exists", "该日期已存在煤炭价格")
    if isinstance(exc, coal_price_service.CoalPriceNotFound):
        return api_error(404, "coal_price_not_found", "未找到该日期的煤炭价格")
    return api_error(400, "coal_price_invalid", "煤炭数据无效")


@router.get("/catalog")
async def etf_share_catalog():
    return catalog_payload()


@router.get("/curve")
async def etf_share_curve(
    group: str = Query(default="hs300"),
    days: int = Query(default=90, ge=5, le=1200),
    full: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    try:
        return await asyncio.to_thread(
            lambda: _load_curve(db, group, days, force=False, full=full)
        )
    except ValueError as exc:
        raise api_error(400, "etf_share_group_invalid", "未知的 ETF 分组") from exc


@router.post("/refresh")
async def etf_share_refresh(
    group: str = Query(default="hs300"),
    days: int = Query(default=90, ge=5, le=1200),
    full: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    try:
        return await asyncio.to_thread(
            lambda: _load_curve(db, group, days, force=True, full=full)
        )
    except ValueError as exc:
        raise api_error(400, "etf_share_group_invalid", "未知的 ETF 分组") from exc


@router.post("/coal/points")
async def create_coal_price_point(
    command: CoalPricePointCommand,
    db: Session = Depends(get_db),
):
    try:
        return await asyncio.to_thread(
            lambda: coal_price_service.create_point(db, command.date, command.price)
        )
    except ValueError as exc:
        raise _coal_point_error(exc) from exc


@router.put("/coal/points/{original_day}")
async def update_coal_price_point(
    original_day: str,
    command: CoalPricePointCommand,
    db: Session = Depends(get_db),
):
    try:
        return await asyncio.to_thread(
            lambda: coal_price_service.update_point(
                db, original_day, command.date, command.price
            )
        )
    except ValueError as exc:
        raise _coal_point_error(exc) from exc
