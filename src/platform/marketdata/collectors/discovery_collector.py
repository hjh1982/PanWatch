import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HotStock:
    symbol: str
    market: str
    name: str
    price: float | None
    change_pct: float | None
    turnover: float | None
    volume: float | None


@dataclass(frozen=True)
class HotBoard:
    code: str
    name: str
    change_pct: float | None
    change_amount: float | None
    turnover: float | None


@dataclass(frozen=True)
class MarketMapStock:
    symbol: str
    market: str
    name: str
    price: float | None
    change_pct: float | None
    total_market_value: float | None
    industry: str | None
    change_amount: float | None = None


@dataclass(frozen=True)
class MarketMapSnapshot:
    stocks: list[MarketMapStock]
    total: int
    up: int
    flat: int
    down: int
    amount: float | None
    as_of: int | None


def get_market_data():
    """惰性导入,避免模块加载时的循环依赖(便于测试 monkeypatch)。"""
    from src.platform.marketdata.marketdata_client import get_market_data as _g

    return _g()


class EastMoneyDiscoveryCollector:
    """Discovery ranks (CN/HK/US),经 marketdata 包统一取数。"""

    def __init__(self, *, proxy: str | None = None):
        self.proxy = proxy

    async def fetch_hot_stocks(
        self,
        *,
        market: str = "CN",
        mode: str = "turnover",
        limit: int = 20,
    ) -> list[HotStock]:
        import asyncio as _aio

        pkg_items = await _aio.to_thread(
            get_market_data().hot_stocks,
            market=market,
            mode=mode,
            limit=limit,
            proxy=self.proxy,
        )
        return [
            HotStock(
                symbol=it.symbol,
                market=it.market,
                name=it.name,
                price=it.price,
                change_pct=it.change_pct,
                turnover=it.turnover,
                volume=it.volume,
            )
            for it in pkg_items
        ]

    async def fetch_hot_boards(
        self,
        *,
        market: str = "CN",
        mode: str = "gainers",
        limit: int = 12,
    ) -> list[HotBoard]:
        import asyncio as _aio

        pkg_items = await _aio.to_thread(
            get_market_data().hot_boards,
            market=market,
            mode=mode,
            limit=limit,
            proxy=self.proxy,
        )
        return [
            HotBoard(
                code=it.code,
                name=it.name,
                change_pct=it.change_pct,
                change_amount=it.change_amount,
                turnover=it.turnover,
            )
            for it in pkg_items
        ]

    async def fetch_market_map(
        self,
        *,
        market: str = "CN",
        limit: int = 400,
    ) -> MarketMapSnapshot:
        import asyncio as _aio

        snapshot = await _aio.to_thread(
            get_market_data().market_map,
            market=market,
            limit=limit,
            proxy=self.proxy,
        )
        return MarketMapSnapshot(
            stocks=[
                MarketMapStock(
                    symbol=it.symbol,
                    market=it.market,
                    name=it.name,
                    price=it.price,
                    change_pct=it.change_pct,
                    total_market_value=it.total_market_value,
                    industry=it.industry,
                    change_amount=it.change_amount,
                )
                for it in snapshot.stocks
            ],
            total=snapshot.total,
            up=snapshot.up,
            flat=snapshot.flat,
            down=snapshot.down,
            amount=snapshot.amount,
            as_of=snapshot.as_of,
        )

    async def fetch_board_stocks(
        self,
        *,
        board_code: str,
        mode: str = "gainers",
        limit: int = 20,
    ) -> list[HotStock]:
        import asyncio as _aio

        pkg_items = await _aio.to_thread(
            get_market_data().board_stocks,
            board_code=board_code,
            mode=mode,
            limit=limit,
            proxy=self.proxy,
        )
        return [
            HotStock(
                symbol=it.symbol,
                market=it.market,
                name=it.name,
                price=it.price,
                change_pct=it.change_pct,
                turnover=it.turnover,
                volume=it.volume,
            )
            for it in pkg_items
        ]
