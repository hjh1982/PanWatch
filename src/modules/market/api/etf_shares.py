"""ETF 份额与融资融券 API：目录、曲线与手动刷新。"""

import asyncio
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.modules.market import etf_share_service, margin_service
from src.modules.market.etf_share_service import catalog_payload
from src.platform.persistence.database import get_db
from src.web.errors import api_error

logger = logging.getLogger(__name__)
router = APIRouter()


def _load_curve(db: Session, group: str, days: int, *, force: bool):
    if group == margin_service.MARGIN_GROUP_ID:
        return margin_service.get_curve_payload(group, days, force=force)
    return etf_share_service.get_curve_payload(db, group, days, force=force)


@router.get("/catalog")
async def etf_share_catalog():
    return catalog_payload()


@router.get("/curve")
async def etf_share_curve(
    group: str = Query(default="hs300"),
    days: int = Query(default=90, ge=5, le=250),
    db: Session = Depends(get_db),
):
    try:
        return await asyncio.to_thread(
            lambda: _load_curve(db, group, days, force=False)
        )
    except ValueError as exc:
        raise api_error(400, "etf_share_group_invalid", "未知的 ETF 分组") from exc


@router.post("/refresh")
async def etf_share_refresh(
    group: str = Query(default="hs300"),
    days: int = Query(default=90, ge=5, le=250),
    db: Session = Depends(get_db),
):
    try:
        return await asyncio.to_thread(
            lambda: _load_curve(db, group, days, force=True)
        )
    except ValueError as exc:
        raise api_error(400, "etf_share_group_invalid", "未知的 ETF 分组") from exc
