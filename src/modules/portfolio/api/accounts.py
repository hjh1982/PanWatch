"""账户和持仓管理 API"""
import asyncio
import logging
import time
import httpx
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel

from datetime import datetime, timedelta, timezone

from src.modules.portfolio.fees import calc_fee
from src.platform.persistence.database import get_db
from src.platform.persistence.models import Account, AccountTransaction, PriceAlertRule, Position, Stock
from src.platform.marketdata.marketdata_client import md_quote_rows
from src.platform.marketdata.collectors.market_http import TTLCache
from src.platform.marketdata.models import MarketCode
from src.platform.marketdata.quote_display import daily_quote_fields
from src.web.errors import ai_api_error, api_error

logger = logging.getLogger(__name__)
router = APIRouter()

# 汇率缓存
_hkd_rate_cache: dict = {"rate": 0.92, "ts": 0}  # 港币默认汇率 0.92
_usd_rate_cache: dict = {"rate": 7.25, "ts": 0}  # 美元默认汇率 7.25
# 持仓事件日历缓存
_calendar_cache: dict = {"ts": 0, "data": None}
EXCHANGE_RATE_TTL = 3600  # 1 小时缓存


def get_hkd_cny_rate() -> float:
    """获取港币兑人民币汇率"""
    global _hkd_rate_cache

    # 检查缓存
    if time.time() - _hkd_rate_cache["ts"] < EXCHANGE_RATE_TTL:
        return _hkd_rate_cache["rate"]

    # 从新浪财经获取汇率
    try:
        resp = httpx.get(
            "https://hq.sinajs.cn/list=fx_shkdcny",
            timeout=5,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/"
            }
        )
        # 格式: var hq_str_fx_shkdcny="时间,汇率,..."
        text = resp.text
        if "=" in text and "," in text:
            data = text.split('"')[1]
            parts = data.split(",")
            if len(parts) > 1:
                rate = float(parts[1])
                _hkd_rate_cache = {"rate": rate, "ts": time.time()}
                logger.info(f"更新港币汇率: {rate}")
                return rate
    except Exception as e:
        logger.warning(f"获取港币汇率失败，使用缓存: {e}")

    return _hkd_rate_cache["rate"]


def get_usd_cny_rate() -> float:
    """获取美元兑人民币汇率"""
    global _usd_rate_cache

    # 检查缓存
    if time.time() - _usd_rate_cache["ts"] < EXCHANGE_RATE_TTL:
        return _usd_rate_cache["rate"]

    # 从新浪财经获取汇率
    try:
        resp = httpx.get(
            "https://hq.sinajs.cn/list=fx_susdcny",
            timeout=5,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/"
            }
        )
        # 格式: var hq_str_fx_susdcny="时间,汇率,..."
        text = resp.text
        if "=" in text and "," in text:
            data = text.split('"')[1]
            parts = data.split(",")
            if len(parts) > 1:
                rate = float(parts[1])
                _usd_rate_cache = {"rate": rate, "ts": time.time()}
                logger.info(f"更新美元汇率: {rate}")
                return rate
    except Exception as e:
        logger.warning(f"获取美元汇率失败，使用缓存: {e}")

    return _usd_rate_cache["rate"]


# ========== Pydantic Models ==========

class AccountCreate(BaseModel):
    name: str
    available_funds: float = 0
    commission_rate: float = 0.00025  # 佣金费率，默认万2.5
    commission_min: float = 5.0  # 最低佣金，0 表示无最低


class AccountUpdate(BaseModel):
    name: str | None = None
    available_funds: float | None = None
    enabled: bool | None = None
    commission_rate: float | None = None
    commission_min: float | None = None


class AccountResponse(BaseModel):
    id: int
    name: str
    available_funds: float
    enabled: bool
    commission_rate: float = 0.00025
    commission_min: float = 5.0

    class Config:
        from_attributes = True


class PositionCreate(BaseModel):
    account_id: int
    stock_id: int
    cost_price: float
    quantity: int
    invested_amount: float | None = None
    trading_style: str | None = None  # short: 短线, swing: 波段, long: 长线
    fee: float = 0.0  # 手续费
    traded_at: datetime | None = None  # 交易时间，缺省为当前时间
    note: str | None = None  # 备注


class PositionUpdate(BaseModel):
    cost_price: float | None = None
    quantity: int | None = None
    invested_amount: float | None = None
    trading_style: str | None = None
    # 动作类型：buy 买入加仓 / sell 卖出减仓 / adjust 仅修正信息（默认）
    action: str = "adjust"
    trade_quantity: int | None = None  # 本次成交数量（buy/sell）
    trade_price: float | None = None  # 本次成交价格（buy/sell）
    fee: float = 0.0
    traded_at: datetime | None = None
    note: str | None = None


class TransactionResponse(BaseModel):
    id: int
    account_id: int
    stock_id: int
    side: str
    quantity: int
    price: float
    amount: float
    fee: float
    realized_pnl: float | None
    traded_at: datetime | None
    note: str | None
    stock_symbol: str | None = None
    stock_name: str | None = None

    class Config:
        from_attributes = True


class PositionResponse(BaseModel):
    id: int
    account_id: int
    stock_id: int
    cost_price: float
    quantity: int
    invested_amount: float | None
    sort_order: int
    trading_style: str | None
    # 关联信息
    account_name: str | None = None
    stock_symbol: str | None = None
    stock_name: str | None = None

    class Config:
        from_attributes = True


class PositionReorderItem(BaseModel):
    id: int
    sort_order: int


class PositionReorderRequest(BaseModel):
    items: list[PositionReorderItem]


# ========== Account Endpoints ==========

@router.get("/accounts", response_model=list[AccountResponse])
def list_accounts(db: Session = Depends(get_db)):
    """获取所有账户"""
    return db.query(Account).order_by(Account.id).all()


@router.get("/accounts/{account_id}", response_model=AccountResponse)
def get_account(account_id: int, db: Session = Depends(get_db)):
    """获取单个账户"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise api_error(404, "account_not_found", "账户不存在")
    return account


@router.get("/accounts/{account_id}/transactions")
def list_account_transactions(account_id: int, db: Session = Depends(get_db)):
    """获取某账户的买卖明细流水（按交易时间倒序），并附买入/卖出/已实现盈亏汇总。"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise api_error(404, "account_not_found", "账户不存在")

    rows = (
        db.query(AccountTransaction)
        .filter(AccountTransaction.account_id == account_id)
        .order_by(
            AccountTransaction.traded_at.desc(),
            AccountTransaction.id.desc(),
        )
        .all()
    )
    items = [
        {
            "id": txn.id,
            "account_id": txn.account_id,
            "stock_id": txn.stock_id,
            "side": txn.side,
            "quantity": txn.quantity,
            "price": txn.price,
            "amount": txn.amount,
            "fee": txn.fee or 0.0,
            "realized_pnl": txn.realized_pnl,
            "traded_at": txn.traded_at,
            "note": txn.note or "",
            "stock_symbol": txn.stock.symbol if txn.stock else None,
            "stock_name": txn.stock.name if txn.stock else None,
        }
        for txn in rows
    ]
    summary = {
        "buy_amount": sum(t["amount"] for t in items if t["side"] == "buy"),
        "sell_amount": sum(t["amount"] for t in items if t["side"] == "sell"),
        "total_fee": sum(t["fee"] for t in items),
        "realized_pnl": sum(t["realized_pnl"] or 0 for t in items),
    }
    return {"items": items, "summary": summary}


@router.post("/accounts", response_model=AccountResponse)
def create_account(data: AccountCreate, db: Session = Depends(get_db)):
    """创建账户"""
    account = Account(
        name=data.name,
        available_funds=data.available_funds,
        commission_rate=data.commission_rate,
        commission_min=data.commission_min,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    logger.info(f"创建账户: {account.name}")
    return account


@router.put("/accounts/{account_id}", response_model=AccountResponse)
def update_account(account_id: int, data: AccountUpdate, db: Session = Depends(get_db)):
    """更新账户"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise api_error(404, "account_not_found", "账户不存在")

    if data.name is not None:
        account.name = data.name
    if data.available_funds is not None:
        account.available_funds = data.available_funds
    if data.enabled is not None:
        account.enabled = data.enabled
    if data.commission_rate is not None:
        account.commission_rate = data.commission_rate
    if data.commission_min is not None:
        account.commission_min = data.commission_min

    db.commit()
    db.refresh(account)
    logger.info(f"更新账户: {account.name}")
    return account


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    """删除账户（会同时删除该账户的所有持仓）"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise api_error(404, "account_not_found", "账户不存在")

    # Read relationship-independent values before commit.  SQLAlchemy expires
    # and detaches deleted instances, so accessing ``account.name`` after the
    # commit may trigger a lazy load and raise DetachedInstanceError.
    account_name = account.name
    db.delete(account)
    db.commit()
    logger.info("删除账户: %s", account_name)
    return {"success": True}


# ========== Position Endpoints ==========

@router.get("/positions", response_model=list[PositionResponse])
def list_positions(
    account_id: int | None = None,
    stock_id: int | None = None,
    db: Session = Depends(get_db)
):
    """获取持仓列表，可按账户或股票筛选"""
    query = db.query(Position)
    if account_id:
        query = query.filter(Position.account_id == account_id)
    if stock_id:
        query = query.filter(Position.stock_id == stock_id)

    positions = query.order_by(Position.account_id.asc(), Position.sort_order.asc(), Position.id.asc()).all()
    result = []
    for pos in positions:
        result.append({
            "id": pos.id,
            "account_id": pos.account_id,
            "stock_id": pos.stock_id,
            "cost_price": pos.cost_price,
            "quantity": pos.quantity,
            "invested_amount": pos.invested_amount,
            "sort_order": pos.sort_order or 0,
            "trading_style": pos.trading_style,
            "account_name": pos.account.name if pos.account else None,
            "stock_symbol": pos.stock.symbol if pos.stock else None,
            "stock_name": pos.stock.name if pos.stock else None,
        })
    return result


@router.post("/positions", response_model=PositionResponse)
def create_position(data: PositionCreate, db: Session = Depends(get_db)):
    """创建持仓"""
    # 检查账户和股票是否存在
    account = db.query(Account).filter(Account.id == data.account_id).first()
    if not account:
        raise api_error(400, "account_not_found", "账户不存在")

    stock = db.query(Stock).filter(Stock.id == data.stock_id).first()
    if not stock:
        raise api_error(400, "stock_not_found", "股票不存在")

    # 检查是否已存在该账户的该股票持仓
    existing = db.query(Position).filter(
        Position.account_id == data.account_id,
        Position.stock_id == data.stock_id,
    ).first()
    if existing:
        raise api_error(400, "position_already_exists", f"账户 {account.name} 已有 {stock.name} 的持仓，请编辑现有持仓")

    max_order = db.query(func.max(Position.sort_order)).filter(
        Position.account_id == data.account_id
    ).scalar() or 0

    position = Position(
        account_id=data.account_id,
        stock_id=data.stock_id,
        cost_price=data.cost_price,
        quantity=data.quantity,
        invested_amount=data.invested_amount,
        sort_order=int(max_order) + 1,
        trading_style=data.trading_style,
    )
    # 手续费按市场规则自动计算（以账户佣金费率为准）
    amount = data.cost_price * data.quantity
    fee = calc_fee(
        stock.market, "buy", amount,
        account.commission_rate or 0.00025, account.commission_min or 0.0,
    )["fee"]
    # 首买手续费计入成本，保证现金口径与已实现盈亏一致
    if fee:
        position.cost_price = data.cost_price + fee / data.quantity
    db.add(position)
    # 新建持仓即第一笔买入：写买入流水，账户现金减少（金额+手续费）
    db.add(AccountTransaction(
        account_id=data.account_id,
        stock_id=data.stock_id,
        side="buy",
        quantity=data.quantity,
        price=data.cost_price,
        amount=amount,
        fee=fee,
        realized_pnl=None,
        traded_at=data.traded_at or datetime.now(),
        note=(data.note or "").strip(),
    ))
    account.available_funds = (account.available_funds or 0) - amount - fee
    db.commit()
    db.refresh(position)

    logger.info(f"创建持仓: {account.name} - {stock.name}")
    return {
        "id": position.id,
        "account_id": position.account_id,
        "stock_id": position.stock_id,
        "cost_price": position.cost_price,
        "quantity": position.quantity,
        "invested_amount": position.invested_amount,
        "sort_order": position.sort_order or 0,
        "trading_style": position.trading_style,
        "account_name": account.name,
        "stock_symbol": stock.symbol,
        "stock_name": stock.name,
    }


@router.put("/positions/{position_id}", response_model=PositionResponse)
def update_position(position_id: int, data: PositionUpdate, db: Session = Depends(get_db)):
    """更新持仓"""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise api_error(404, "position_not_found", "持仓不存在")

    account = position.account
    account_name = account.name if account else None
    account_id = position.account_id
    stock_id = position.stock_id
    cost_price = position.cost_price
    stock_symbol = position.stock.symbol if position.stock else None
    stock_name = position.stock.name if position.stock else None
    stock_market = position.stock.market if position.stock else None
    deleted = False

    if data.action == "buy":
        # 买入加仓：加权平均成本（手续费计入成本），写流水，现金减少
        qty = data.trade_quantity or 0
        price = data.trade_price
        if qty <= 0 or price is None or price <= 0:
            raise api_error(400, "invalid_trade", "买入数量或价格无效")
        amount = price * qty
        fee = calc_fee(
            stock_market, "buy", amount,
            account.commission_rate if account else 0.00025,
            account.commission_min if account else 5.0,
        )["fee"]
        old_qty = position.quantity or 0
        new_qty = old_qty + qty
        position.cost_price = (
            position.cost_price * old_qty + price * qty + fee
        ) / new_qty
        position.quantity = new_qty
        db.add(AccountTransaction(
            account_id=position.account_id,
            stock_id=position.stock_id,
            side="buy",
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            realized_pnl=None,
            traded_at=data.traded_at or datetime.now(),
            note=(data.note or "").strip(),
        ))
        if account:
            account.available_funds = (account.available_funds or 0) - amount - fee
    elif data.action == "sell":
        # 卖出减仓：剩余成本不变；已实现盈亏=(卖价-成本)×数量-手续费；现金增加
        qty = data.trade_quantity or 0
        price = data.trade_price
        if qty <= 0 or price is None or price <= 0:
            raise api_error(400, "invalid_trade", "卖出数量或价格无效")
        if qty > position.quantity:
            raise api_error(400, "invalid_trade", "卖出数量超过当前持仓")
        amount = price * qty
        fee = calc_fee(
            stock_market, "sell", amount,
            account.commission_rate if account else 0.00025,
            account.commission_min if account else 5.0,
        )["fee"]
        avg_cost = position.cost_price
        realized = (price - avg_cost) * qty - fee
        remaining = position.quantity - qty
        db.add(AccountTransaction(
            account_id=position.account_id,
            stock_id=position.stock_id,
            side="sell",
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            realized_pnl=realized,
            traded_at=data.traded_at or datetime.now(),
            note=(data.note or "").strip(),
        ))
        if account:
            account.available_funds = (account.available_funds or 0) + amount - fee
        if remaining == 0:
            db.delete(position)
            deleted = True
        else:
            position.quantity = remaining
    else:
        # adjust：仅修正信息，不动现金、不产生流水
        if data.cost_price is not None:
            position.cost_price = data.cost_price
        if data.quantity is not None:
            position.quantity = data.quantity
        if data.invested_amount is not None:
            position.invested_amount = data.invested_amount
        if data.trading_style is not None:
            # 空字符串表示清空，设为 None
            position.trading_style = data.trading_style if data.trading_style else None

    db.commit()
    if not deleted:
        db.refresh(position)

    logger.info(f"更新持仓({data.action}): {account_name} - {stock_name}")
    return {
        "id": position_id,
        "account_id": account_id,
        "stock_id": stock_id,
        "cost_price": position.cost_price if not deleted else cost_price,
        "quantity": 0 if deleted else position.quantity,
        "invested_amount": None if deleted else position.invested_amount,
        "sort_order": 0 if deleted else (position.sort_order or 0),
        "trading_style": None if deleted else position.trading_style,
        "account_name": account_name,
        "stock_symbol": stock_symbol,
        "stock_name": stock_name,
    }


@router.delete("/positions/{position_id}")
def delete_position(position_id: int, db: Session = Depends(get_db)):
    """删除持仓"""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise api_error(404, "position_not_found", "持仓不存在")

    # Capture lazy relationships before the row is deleted/committed.  The
    # deleted Position is no longer session-bound afterwards; logging its
    # relationships at that point can raise DetachedInstanceError.
    account_name = position.account.name if position.account else "未知账户"
    stock_name = position.stock.name if position.stock else "未知股票"
    db.delete(position)
    db.commit()
    logger.info("删除持仓: %s - %s", account_name, stock_name)
    return {"success": True}


@router.put("/positions/reorder/batch")
def reorder_positions(data: PositionReorderRequest, db: Session = Depends(get_db)):
    """批量更新持仓排序"""
    if not data.items:
        return {"updated": 0}
    ids = [int(x.id) for x in data.items]
    rows = db.query(Position).filter(Position.id.in_(ids)).all()
    row_map = {r.id: r for r in rows}
    updated = 0
    for item in data.items:
        row = row_map.get(int(item.id))
        if not row:
            continue
        row.sort_order = int(item.sort_order)
        updated += 1
    db.commit()
    return {"updated": updated}


# ========== Portfolio Summary ==========

@router.get("/portfolio/summary")
def get_portfolio_summary(
    account_id: int | None = None,
    include_quotes: bool = True,
    refresh_exchange_rates: bool = True,
    db: Session = Depends(get_db),
):
    """
    获取持仓汇总信息

    Args:
        account_id: 可选，指定账户ID。不指定则汇总所有账户

    Returns:
        accounts: 账户列表及各账户持仓明细
        total: 所有账户汇总
    """
    # 获取账户
    if account_id:
        accounts = db.query(Account).filter(Account.id == account_id, Account.enabled == True).all()
    else:
        accounts = db.query(Account).filter(Account.enabled == True).all()

    if not accounts:
        return {
            "accounts": [],
            "total": {
                "total_market_value": 0,
                "total_cost": 0,
                "total_pnl": 0,
                "total_pnl_pct": 0,
                "available_funds": 0,
                "total_assets": 0,
            }
        }

    # 获取所有相关股票
    all_stock_ids = set()
    for acc in accounts:
        for pos in acc.positions:
            all_stock_ids.add(pos.stock_id)

    stocks = db.query(Stock).filter(Stock.id.in_(all_stock_ids)).all() if all_stock_ids else []
    stock_map = {s.id: s for s in stocks}

    # 获取实时行情（可选）
    quotes = _fetch_quotes_for_stocks(stocks) if include_quotes else {}

    # 保存后的元数据刷新可复用汇率缓存,避免等待外部源。
    markets = {stock.market for stock in stocks}
    hkd_rate = get_hkd_cny_rate() if refresh_exchange_rates and "HK" in markets else _hkd_rate_cache["rate"]
    usd_rate = get_usd_cny_rate() if refresh_exchange_rates and "US" in markets else _usd_rate_cache["rate"]

    # 计算各账户持仓
    account_summaries = []
    grand_total_market_value = 0
    grand_total_cost = 0
    grand_available_funds = 0
    grand_daily_pnl = 0

    for acc in accounts:
        positions_data = []
        acc_market_value = 0
        acc_cost = 0
        acc_daily_pnl = 0

        positions_sorted = sorted(
            list(acc.positions or []),
            key=lambda p: (int(getattr(p, "sort_order", 0) or 0), int(p.id)),
        )
        for pos in positions_sorted:
            stock = stock_map.get(pos.stock_id)
            if not stock:
                continue

            quote = quotes.get(stock.symbol)
            current_price = quote["current_price"] if quote else None
            daily_fields = daily_quote_fields(stock.market, quote)
            change_pct = daily_fields["change_pct"]
            prev_close = quote.get("prev_close") if quote else None

            # 根据市场确定汇率
            is_foreign = stock.market in ("HK", "US")
            if stock.market == "HK":
                rate = hkd_rate
            elif stock.market == "US":
                rate = usd_rate
            else:
                rate = 1.0

            market_value = None
            market_value_cny = None
            pnl = None
            pnl_pct = None
            daily_pnl = None
            daily_pnl_pct = None

            if daily_fields["daily_move_status"] in ("closed", "pre_market"):
                daily_pnl = 0.0
            elif daily_fields["daily_move_status"] == "current" and current_price is not None and prev_close and prev_close > 0:
                daily_pnl = (current_price - prev_close) * pos.quantity * rate
                daily_pnl_pct = (current_price - prev_close) / prev_close * 100
                acc_daily_pnl += daily_pnl

            cost = pos.cost_price * pos.quantity
            cost_cny = cost * rate  # 假设成本价也是原币种
            acc_cost += cost_cny

            if current_price is not None:
                market_value = current_price * pos.quantity  # 原币种市值
                market_value_cny = market_value * rate  # 人民币市值
                pnl = market_value_cny - cost_cny
                pnl_pct = (pnl / cost_cny * 100) if cost_cny > 0 else 0

                acc_market_value += market_value_cny

            positions_data.append({
                "id": pos.id,
                "stock_id": pos.stock_id,
                "symbol": stock.symbol,
                "name": stock.name,
                "market": stock.market,
                "cost_price": pos.cost_price,
                "quantity": pos.quantity,
                "invested_amount": pos.invested_amount,
                "sort_order": pos.sort_order or 0,
                "trading_style": pos.trading_style,
                "current_price": current_price,
                "current_price_cny": round(current_price * rate, 2) if current_price else None,
                "change_pct": change_pct,
                "market_value": round(market_value, 2) if market_value else None,
                "market_value_cny": round(market_value_cny, 2) if market_value_cny else None,
                "pnl": round(pnl, 2) if pnl else None,
                "pnl_pct": round(pnl_pct, 2) if pnl_pct else None,
                "daily_pnl": round(daily_pnl, 2) if daily_pnl is not None else None,
                "daily_pnl_pct": round(daily_pnl_pct, 2) if daily_pnl_pct is not None else None,
                "exchange_rate": rate if is_foreign else None,
                "daily_move_status": daily_fields["daily_move_status"],
                "quote_date": daily_fields["quote_date"],
            })

        if include_quotes:
            acc_pnl = acc_market_value - acc_cost
            acc_pnl_pct = (acc_pnl / acc_cost * 100) if acc_cost > 0 else 0
            acc_total_assets = acc_market_value + acc.available_funds
        else:
            acc_pnl = 0
            acc_pnl_pct = 0
            acc_total_assets = acc.available_funds

        # 持仓按市值（人民币）从大到小排序；无行情时回退到手动排序
        positions_data.sort(
            key=lambda p: (
                -(p["market_value_cny"] or 0),
                p["sort_order"] or 0,
                p["id"],
            )
        )

        account_summaries.append({
            "id": acc.id,
            "name": acc.name,
            "available_funds": acc.available_funds,
            "commission_rate": acc.commission_rate if acc.commission_rate is not None else 0.00025,
            "commission_min": acc.commission_min if acc.commission_min is not None else 5.0,
            "total_market_value": round(acc_market_value, 2),
            "total_cost": round(acc_cost, 2),
            "total_pnl": round(acc_pnl, 2),
            "total_pnl_pct": round(acc_pnl_pct, 2),
            "total_daily_pnl": round(acc_daily_pnl, 2),
            "total_assets": round(acc_total_assets, 2),
            "positions": positions_data,
        })

        grand_total_market_value += acc_market_value
        grand_total_cost += acc_cost
        grand_available_funds += acc.available_funds
        grand_daily_pnl += acc_daily_pnl

    if include_quotes:
        grand_pnl = grand_total_market_value - grand_total_cost
        grand_pnl_pct = (grand_pnl / grand_total_cost * 100) if grand_total_cost > 0 else 0
        grand_total_assets = grand_total_market_value + grand_available_funds
    else:
        grand_pnl = 0
        grand_pnl_pct = 0
        grand_total_assets = grand_available_funds

    # 构建 quotes 字典（用于前端股票列表显示）
    quotes_dict = {}
    if include_quotes:
        for symbol, quote in quotes.items():
            quotes_dict[symbol] = {
                "current_price": quote.get("current_price"),
                **daily_quote_fields(quote.get("market", "CN"), quote),
            }

    return {
        "accounts": account_summaries,
        "total": {
            "total_market_value": round(grand_total_market_value, 2),
            "total_cost": round(grand_total_cost, 2),
            "total_pnl": round(grand_pnl, 2),
            "total_pnl_pct": round(grand_pnl_pct, 2),
            "total_daily_pnl": round(grand_daily_pnl, 2),
            "available_funds": round(grand_available_funds, 2),
            "total_assets": round(grand_total_assets, 2),
        },
        "exchange_rates": {
            "HKD_CNY": hkd_rate,
            "USD_CNY": usd_rate,
        },
        "quotes": quotes_dict,  # 可选：返回行情数据
    }


def _fetch_quotes_for_stocks(stocks: list[Stock]) -> dict:
    """获取股票列表的实时行情"""
    if not stocks:
        return {}

    # 按市场分组
    market_stocks: dict[str, list[Stock]] = {}
    for s in stocks:
        market_stocks.setdefault(s.market, []).append(s)

    batches = []
    for market, stock_list in market_stocks.items():
        try:
            market_code = MarketCode(market)
        except ValueError:
            continue

        batches.append((market_code.value, [s.symbol for s in stock_list]))

    def fetch_batch(batch):
        market, symbols = batch
        try:
            return md_quote_rows(symbols, market)
        except Exception as e:
            logger.error(f"获取 {market} 行情失败: {e}")
            return []

    # 只把普通行情参数交给线程；数据库 Session/ORM 对象留在调用线程。
    if len(batches) > 1:
        with ThreadPoolExecutor(max_workers=len(batches)) as pool:
            results = list(pool.map(fetch_batch, batches))
    else:
        results = [fetch_batch(batch) for batch in batches]

    return {item["symbol"]: item for items in results for item in items}


# 组合基准/归因结果缓存:重建全持仓 NAV 很贵(逐只拉 K 线),按持仓指纹缓存结果。
# 持仓变动即失效(指纹变);失败/空结果不缓存,避免把瞬时故障冻住 10 分钟。
_PORTFOLIO_RESULT_CACHE = TTLCache(default_ttl_sec=600.0)

# 日内曲线每分钟都在变,缓存只用来压掉同页面多次刷新(30s 内复用)。
_INTRADAY_CACHE = TTLCache(default_ttl_sec=30.0, max_size=64)


def _holdings_signature(db: Session) -> str:
    """启用账户持仓的稳定指纹(stock_id + 合并后数量);仅查 DB,不拉行情/K 线。"""
    rows = (
        db.query(Position.stock_id, Position.quantity)
        .join(Account, Account.id == Position.account_id)
        .filter(Account.enabled == True)  # noqa: E712
        .all()
    )
    agg: dict[int, float] = {}
    for sid, qty in rows:
        agg[sid] = agg.get(sid, 0.0) + (qty or 0)
    return ";".join(f"{sid}:{agg[sid]:g}" for sid in sorted(agg))


def _gather_holdings(db: Session, account_id: int | None = None) -> list[dict]:
    """汇总启用账户的真实持仓为统一列表(CNY 市值/浮盈 + fx),多账户同股合并。

    ``account_id`` 传入时只看该账户;默认(不传)保持原行为:全部启用账户合并。
    """
    query = db.query(Account).filter(Account.enabled == True)  # noqa: E712
    if account_id:
        query = query.filter(Account.id == account_id)
    accounts = query.all()
    stock_ids = {p.stock_id for acc in accounts for p in acc.positions}
    stocks = db.query(Stock).filter(Stock.id.in_(stock_ids)).all() if stock_ids else []
    stock_map = {s.id: s for s in stocks}
    quotes = _fetch_quotes_for_stocks(stocks) if stocks else {}
    markets = {stock.market for stock in stocks}
    hkd = get_hkd_cny_rate() if "HK" in markets else 1.0
    usd = get_usd_cny_rate() if "US" in markets else 1.0

    out: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    for acc in accounts:
        for pos in acc.positions:
            stock = stock_map.get(pos.stock_id)
            if not stock:
                continue
            rate = hkd if stock.market == "HK" else usd if stock.market == "US" else 1.0
            quote = quotes.get(stock.symbol)
            price = quote.get("current_price") if quote else None
            cost_cny = pos.cost_price * pos.quantity * rate
            mv_cny = (price * pos.quantity * rate) if price else cost_cny
            pnl_cny = (mv_cny - cost_cny) if price else 0.0
            key = (stock.market, stock.symbol)
            if key in seen:  # 多账户同一标的合并
                h = seen[key]
                h["quantity"] += pos.quantity
                h["market_value"] += mv_cny
                h["unrealized_pnl"] += pnl_cny
            else:
                h = {
                    "symbol": stock.symbol,
                    "market": stock.market,
                    "name": stock.name,
                    "quantity": pos.quantity,
                    "fx": rate,
                    "market_value": mv_cny,
                    "unrealized_pnl": pnl_cny,
                    "strategy_code": pos.trading_style or "",
                }
                seen[key] = h
                out.append(h)
    return out


@router.get("/portfolio/calendar")
def portfolio_calendar(db: Session = Depends(get_db)):
    """持仓未来事件日历：分红登记/除息、限售解禁、财报截止日（60 秒缓存）。"""
    from src.modules.portfolio import upcoming_events as ue

    global _calendar_cache
    if _calendar_cache["ts"] and time.time() - _calendar_cache["ts"] < 60:
        return _calendar_cache["data"]

    # 汇总全部持仓（去重），按市场分组
    summary = get_portfolio_summary(include_quotes=False,
                                    refresh_exchange_rates=False, db=db)
    cn_symbols: list[str] = []
    hk_symbols: list[str] = []
    name_map: dict[str, str] = {}
    for acc in summary["accounts"]:
        for pos in acc["positions"]:
            symbol = pos["symbol"]
            name_map[symbol] = pos["name"]
            if pos["market"] == "CN" and symbol not in cn_symbols:
                cn_symbols.append(symbol)
            elif pos["market"] == "HK" and symbol not in hk_symbols:
                hk_symbols.append(symbol)

    today = datetime.now().date()
    events: list[dict] = []
    if cn_symbols:
        events += ue.dividend_events(cn_symbols, name_map, today)
        events += ue.lift_events(cn_symbols, name_map, today)
        events += ue.disclosure_deadlines("CN", today)
        # 个股财报预约披露日（仅 A 股个股，排除 ETF）
        meta = [
            (
                s,
                name_map.get(s, s),
                ("SH" if s.startswith(("6", "9")) else "SZ") + s,
            )
            for s in cn_symbols
            if s[0] in "036"
        ]
        events += ue.disclosure_events(meta, today)
    if hk_symbols:
        events += ue.disclosure_deadlines("HK", today)

    # 同事件去重（类型+日期+代码+标题）
    seen: set = set()
    uniq: list[dict] = []
    for ev in events:
        key = (ev["date"], ev["type"], ev["symbol"], ev["title"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(ev)
    uniq.sort(key=lambda e: (e["date"], e["type"], e["symbol"]))

    data = {"events": uniq}
    _calendar_cache = {"ts": time.time(), "data": data}
    return data


@router.get("/portfolio/diagnostics")
def portfolio_diagnostics(db: Session = Depends(get_db)):
    """真实持仓组合诊断:集中度(HHI)/最大单仓/市场分布/风险提示(只读)。"""
    from src.modules.portfolio.portfolio_diagnostics import diagnose_positions

    return diagnose_positions(_gather_holdings(db))


@router.get("/portfolio/benchmark")
def portfolio_benchmark(
    days: int = 60, benchmark: str = "000300", db: Session = Depends(get_db)
):
    """真实持仓组合 vs 基准:超额收益/信息比率/相对回撤 + 归一化净值曲线。"""
    from src.modules.portfolio.portfolio_benchmark import (
        DEFAULT_BENCHMARK,
        build_portfolio_benchmark,
    )

    days = max(20, min(int(days), 250))
    bcode = benchmark or DEFAULT_BENCHMARK
    sig = _holdings_signature(db)
    if not sig:
        return {"empty": True, "reason": "no_holdings"}
    ckey = f"bench:{days}:{bcode}:{sig}"
    cached = _PORTFOLIO_RESULT_CACHE.get(ckey)
    if cached is not None:
        return cached

    holdings = _gather_holdings(db)
    if not holdings:
        return {"empty": True, "reason": "no_holdings"}
    res = build_portfolio_benchmark(holdings, days=days, benchmark_code=bcode)
    if not res:
        # 失败/数据不足不缓存,下轮可重试(由 K 线负缓存兜住打爆)
        return {"empty": True, "reason": "insufficient_data"}
    _PORTFOLIO_RESULT_CACHE.set(ckey, res)
    return res


@router.get("/portfolio/intraday-curve")
def portfolio_intraday_curve(account_id: int | None = None, db: Session = Depends(get_db)):
    """持仓日内市值曲线:当天逐分钟 Σ(持仓量 × 分时价 × 汇率)。

    只描述**当天**盘中变化,不涉及历史重算;当日休市的标的(如港股假期)
    按最新价恒定计入并由 ``coverage.flat`` 说明,不虚构日内波动。
    """
    from src.modules.portfolio.portfolio_intraday import build_intraday_curve
    from src.platform.marketdata.collectors.intraday_collector import get_intraday_many

    holdings = _gather_holdings(db, account_id)
    if not holdings:
        return {
            "empty": True,
            "reason": "no_holdings",
            "session_date": None,
            "points": [],
            "summary": None,
        }

    cash_query = db.query(func.sum(Account.available_funds)).filter(
        Account.enabled == True  # noqa: E712
    )
    if account_id:
        cash_query = cash_query.filter(Account.id == account_id)
    cash = float(cash_query.scalar() or 0.0)

    symbols = [h["symbol"] for h in holdings]
    ckey = (
        "intraday:"
        + "|".join(sorted(f"{h['market']}:{h['symbol']}" for h in holdings))
        + f":{cash:.2f}:{account_id or 0}"
    )
    cached = _INTRADAY_CACHE.get(ckey)
    if cached is not None:
        return cached

    series = get_intraday_many([(h["market"], h["symbol"]) for h in holdings])

    # 昨收基准:批量行情(每市场一次),拿不到就退回该标的首个分时价。
    stocks = db.query(Stock).filter(Stock.symbol.in_(symbols)).all() if symbols else []
    quotes = _fetch_quotes_for_stocks(stocks) if stocks else {}
    prev_close = {
        (h["market"], h["symbol"]): (quotes.get(h["symbol"]) or {}).get("prev_close")
        for h in holdings
    }

    res = build_intraday_curve(
        holdings, series=series, prev_close=prev_close, cash=cash
    )
    if not res.get("empty"):
        res["account_id"] = account_id
        res["currency"] = "CNY"
        res["refreshed_at"] = datetime.now(timezone.utc).isoformat()
        _INTRADAY_CACHE.set(ckey, res)
    return res


@router.get("/portfolio/todos")
def portfolio_todos(db: Session = Depends(get_db)):
    """首页空态待办:持仓但未设提醒 / 提醒即将到期(可行动,盘后也不空)。"""
    todos: list[dict] = []
    accounts = db.query(Account).filter(Account.enabled == True).all()  # noqa: E712
    held_ids = {p.stock_id for acc in accounts for p in acc.positions}
    if held_ids:
        ruled = {
            r.stock_id
            for r in db.query(PriceAlertRule)
            .filter(PriceAlertRule.enabled == True, PriceAlertRule.stock_id.in_(held_ids))  # noqa: E712
            .all()
        }
        for sid in held_ids - ruled:
            stock = db.query(Stock).filter(Stock.id == sid).first()
            if stock:
                todos.append(
                    {
                        "type": "no_alert",
                        "symbol": stock.symbol,
                        "market": stock.market,
                        "name": stock.name,
                        "message": f"{stock.name} 持仓中,未设价格提醒",
                    }
                )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    soon = now + timedelta(days=3)
    expiring = (
        db.query(PriceAlertRule)
        .filter(
            PriceAlertRule.enabled == True,  # noqa: E712
            PriceAlertRule.expire_at.isnot(None),
            PriceAlertRule.expire_at >= now,
            PriceAlertRule.expire_at <= soon,
        )
        .all()
    )
    for r in expiring:
        stock = db.query(Stock).filter(Stock.id == r.stock_id).first()
        todos.append(
            {
                "type": "alert_expiring",
                "symbol": stock.symbol if stock else "",
                "market": stock.market if stock else "CN",
                "name": r.name or "",
                "message": f"{(r.name or '提醒')} 即将到期",
            }
        )

    return {"todos": todos[:10], "count": len(todos)}


@router.get("/portfolio/attribution")
def portfolio_attribution(days: int = 60, benchmark: str = "000300", db: Session = Depends(get_db)):
    """近 days 日各持仓对组合收益的贡献(谁拖累/贡献),降序。"""
    from src.modules.portfolio.portfolio_benchmark import DEFAULT_BENCHMARK, build_attribution

    days = max(20, min(int(days), 250))
    bcode = benchmark or DEFAULT_BENCHMARK
    sig = _holdings_signature(db)
    if not sig:
        return {"items": []}
    ckey = f"attr:{days}:{bcode}:{sig}"
    cached = _PORTFOLIO_RESULT_CACHE.get(ckey)
    if cached is not None:
        return cached

    holdings = _gather_holdings(db)
    if not holdings:
        return {"items": []}
    items = build_attribution(holdings, days=days, benchmark_code=bcode)
    result = {"items": items}
    if items:  # 空结果不缓存,下轮可重试
        _PORTFOLIO_RESULT_CACHE.set(ckey, result)
    return result


def _gather_account_totals(db: Session, *, market_value: float) -> dict:
    """Use the same enabled-account scope as holdings; cash is stored in CNY.

    Reuse the already-valued holdings instead of fetching quotes a second time.
    Non-positive equity has no meaningful exposure ratio (not zero exposure).
    """
    cash = float(db.query(func.sum(Account.available_funds)).filter(
        Account.enabled == True  # noqa: E712
    ).scalar() or 0.0)
    total = market_value + cash
    return {
        "available_funds": round(cash, 2),
        "total_assets": round(total, 2),
        "equity_ratio": market_value / total if total > 0 else None,
    }


def _prepare_portfolio_review(model_id, bind):
    """Own the session and all blocking market-data work in the worker."""
    with Session(bind) as db:
        from src.modules.portfolio.portfolio_benchmark import build_attribution, build_portfolio_benchmark
        from src.modules.portfolio.portfolio_diagnostics import diagnose_positions
        from src.platform.ai.ai_failover import get_configured_failover_client
        from src.platform.language import resolve_report_language

        holdings = _gather_holdings(db)
        if not holdings:
            return {"empty": True, "reason": "no_holdings"}

        diag = diagnose_positions(holdings)
        totals = _gather_account_totals(db, market_value=diag["total_market_value"])
        bench = build_portfolio_benchmark(holdings, days=60) or {}
        attr = build_attribution(holdings, days=60)
        top = attr[:3]
        worst = list(reversed(attr[-3:])) if len(attr) > 3 else []

        english = resolve_report_language(db) == "en-US"
        lines = (
            [
                f"{diag['position_count']} holdings; market value {diag['total_market_value']:.0f} CNY; unrealized P&L {diag['total_unrealized_pnl']:.0f} CNY",
                f"Concentration within invested capital: HHI {diag['hhi']}; largest position {diag['max_weight'] * 100:.0f}%",
                f"Total assets across enabled accounts: {totals['total_assets']:.0f} CNY (cash/available funds {totals['available_funds']:.0f} CNY)",
                (f"Total-asset exposure: equities are {totals['equity_ratio'] * 100:.1f}% of total assets"
                 if totals['equity_ratio'] is not None else "Total-asset exposure: unavailable because total assets are not positive"),
            ]
            if english
            else [
                f"持仓 {diag['position_count']} 只,总市值 {diag['total_market_value']:.0f},浮盈 {diag['total_unrealized_pnl']:.0f}",
                f"持仓内部集中度 HHI {diag['hhi']},最大单仓占已投资金额 {diag['max_weight'] * 100:.0f}%",
                f"启用账户总资产 {totals['total_assets']:.0f} CNY（现金/可用资金 {totals['available_funds']:.0f} CNY）",
                (f"总资产敞口：权益类仓位占总资产 {totals['equity_ratio'] * 100:.1f}%"
                 if totals['equity_ratio'] is not None else "总资产敞口：总资产非正，比例不可计算"),
            ]
        )
        if bench.get("excess_return") is not None:
            label = bench.get("benchmark_code") or bench.get("benchmark_label", "benchmark" if english else "基准")
            lines.append(
                (f"Last 60 days vs {label}: excess return {bench['excess_return']}% "
                 f"(portfolio {bench.get('portfolio_return')}% / benchmark {bench.get('benchmark_return')}%); "
                 f"relative drawdown {bench.get('relative_drawdown')}%")
                if english
                else f"近60日 vs {label}:超额 {bench['excess_return']}%"
                     f"(组合 {bench.get('portfolio_return')}% / 基准 {bench.get('benchmark_return')}%),"
                     f"相对回撤 {bench.get('relative_drawdown')}%"
            )
        if diag.get("by_market"):
            prefix = "Market distribution within holdings (CNY): " if english else "持仓内部市场分布（市值 CNY）:"
            lines.append(prefix + ", ".join(f"{k} {v:.0f}" for k, v in diag["by_market"].items()))
        if diag.get("alerts"):
            if english:
                details = diag.get("alert_details") or []
                localized_alerts = []
                for detail in details:
                    if detail.get("code") == "single_concentration":
                        localized_alerts.append(f"largest position is {detail.get('weight')}%")
                    elif detail.get("code") == "hhi_concentration":
                        localized_alerts.append(f"high concentration (HHI={detail.get('hhi')})")
                    elif detail.get("code") == "too_few_positions":
                        localized_alerts.append(f"only {detail.get('count')} holdings")
                    elif detail.get("code") == "market_concentration":
                        localized_alerts.append(f"{detail.get('market')} exposure is {detail.get('weight')}%")
                lines.append("Risk flags: " + "; ".join(localized_alerts))
            else:
                lines.append("风险提示:" + "; ".join(diag["alerts"]))
        if top:
            lines.append(("Top contributors: " if english else "贡献最大:") + ", ".join(f"{r['name']}({r['contribution_pct']:+.2f}%)" for r in top))
        if worst:
            lines.append(("Largest drags: " if english else "拖累最大:") + ", ".join(f"{r['name']}({r['contribution_pct']:+.2f}%)" for r in worst))

        system_prompt = (
            (
                "You are a prudent portfolio adviser. Based on the supplied diagnostics, benchmark comparison, and attribution, write a concise health check and 2-3 actionable rebalancing suggestions in English. Distinguish concentration within invested capital from equity exposure relative to total assets. Never describe total-asset exposure using the within-holdings concentration percentage. Cash is the recorded available balance and is not broker-verified. If total assets are not positive, do not invent an exposure ratio. This is read-only analysis: do not place orders or promise returns. Use exactly this structure:\nHealth check: one-sentence summary\nSuggestions:\n- 2-3 concrete actions\nRisk: the largest risk in one sentence"
                if english
                else "你是稳健的组合顾问。基于给定的组合诊断/基准对比/个股归因,给一段简短体检 + 可执行调仓建议,"
                     "务必区分持仓内部集中度（已投资金额中的分布）和相对总资产的实际权益敞口。"
                     "不得用持仓内部的集中度百分比形容总资产敞口。现金按启用账户已录入的可用资金计算，未核验券商余额。"
                     "总资产非正时不得编造敞口比例；输出必须包含‘持仓内部集中度’和‘总资产敞口’两项。"
                     "只读分析、不下单、不承诺收益。严格格式:\n体检: 一句话总评\n建议:\n- (2~3 条具体可执行)\n风险: 一句话最大风险"
            )
        )
        user_content = ("Portfolio overview:\n" if english else "组合概况:\n") + "\n".join(lines)
        return {"client": get_configured_failover_client(db, model_id), "system_prompt": system_prompt,
                "user_content": user_content, "result": {"top": top, "worst": worst, "diagnostics": diag, "benchmark": bench, "account_totals": totals}}


@router.post("/portfolio/ai-review")
async def portfolio_ai_review(model_id: int | None = None, db: Session = Depends(get_db)):
    prepared = await asyncio.to_thread(_prepare_portfolio_review, model_id, db.get_bind())
    if prepared.get("empty"):
        return prepared
    try:
        content = await prepared["client"].chat(prepared["system_prompt"], prepared["user_content"], temperature=0.3)
    except Exception as e:
        logger.exception("AI 体检失败")
        raise ai_api_error(e) from e
    return {"content": content, **prepared["result"]}
