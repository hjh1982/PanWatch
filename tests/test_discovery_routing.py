"""发现(东财热门榜)取数路由测试:统一走 marketdata 包"""
import asyncio

import src.platform.marketdata.collectors.discovery_collector as dc


def test_fetch_hot_stocks_uses_marketdata(monkeypatch):
    """fetch_hot_stocks 走 marketdata 包的 hot_stocks,转换为本模块 HotStock,且透传 proxy。"""
    from marketdata.types import HotStock as MdHotStock

    captured: dict = {}

    class _MD:
        def hot_stocks(self, *, market="CN", mode="turnover", limit=20, proxy=None):
            captured["market"] = market
            captured["mode"] = mode
            captured["limit"] = limit
            captured["proxy"] = proxy
            return [
                MdHotStock(
                    symbol="600519",
                    market="CN",
                    name="贵州茅台",
                    price=1700.0,
                    change_pct=1.23,
                    turnover=999999.0,
                    volume=1000.0,
                )
            ]

    monkeypatch.setattr(dc, "get_market_data", lambda: _MD())

    collector = dc.EastMoneyDiscoveryCollector(proxy="http://market-scan-proxy:1080")
    out = asyncio.run(collector.fetch_hot_stocks(market="CN", mode="turnover", limit=20))

    assert captured["proxy"] == "http://market-scan-proxy:1080"
    assert captured["market"] == "CN"
    assert captured["mode"] == "turnover"
    assert captured["limit"] == 20

    assert len(out) == 1
    item = out[0]
    assert isinstance(item, dc.HotStock)
    assert item.symbol == "600519"
    assert item.market == "CN"
    assert item.name == "贵州茅台"
    assert item.price == 1700.0
    assert item.change_pct == 1.23
    assert item.turnover == 999999.0
    assert item.volume == 1000.0


def test_fetch_hot_boards_uses_marketdata(monkeypatch):
    """fetch_hot_boards 走 marketdata 包的 hot_boards,转换为本模块 HotBoard,且透传 proxy。"""
    from marketdata.types import HotBoard as MdHotBoard

    captured: dict = {}

    class _MD:
        def hot_boards(self, *, market="CN", mode="gainers", limit=12, proxy=None):
            captured["market"] = market
            captured["mode"] = mode
            captured["limit"] = limit
            captured["proxy"] = proxy
            return [
                MdHotBoard(
                    code="BK0500",
                    name="白酒",
                    change_pct=2.5,
                    change_amount=1.1,
                    turnover=88888.0,
                )
            ]

    monkeypatch.setattr(dc, "get_market_data", lambda: _MD())

    collector = dc.EastMoneyDiscoveryCollector(proxy="http://market-scan-proxy:1080")
    out = asyncio.run(collector.fetch_hot_boards(market="CN", mode="gainers", limit=12))

    assert captured["proxy"] == "http://market-scan-proxy:1080"
    assert captured["market"] == "CN"
    assert captured["mode"] == "gainers"
    assert captured["limit"] == 12

    assert len(out) == 1
    item = out[0]
    assert isinstance(item, dc.HotBoard)
    assert item.code == "BK0500"
    assert item.name == "白酒"
    assert item.change_pct == 2.5
    assert item.change_amount == 1.1
    assert item.turnover == 88888.0


def test_fetch_board_stocks_uses_marketdata(monkeypatch):
    """fetch_board_stocks 走 marketdata 包的 board_stocks,转换为本模块 HotStock,且透传 proxy。"""
    from marketdata.types import HotStock as MdHotStock

    captured: dict = {}

    class _MD:
        def board_stocks(self, *, board_code, mode="gainers", limit=20, proxy=None):
            captured["board_code"] = board_code
            captured["mode"] = mode
            captured["limit"] = limit
            captured["proxy"] = proxy
            return [
                MdHotStock(
                    symbol="000858",
                    market="CN",
                    name="五粮液",
                    price=150.0,
                    change_pct=3.3,
                    turnover=55555.0,
                    volume=200.0,
                )
            ]

    monkeypatch.setattr(dc, "get_market_data", lambda: _MD())

    collector = dc.EastMoneyDiscoveryCollector(proxy="http://market-scan-proxy:1080")
    out = asyncio.run(collector.fetch_board_stocks(board_code="BK0500", mode="gainers", limit=20))

    assert captured["proxy"] == "http://market-scan-proxy:1080"
    assert captured["board_code"] == "BK0500"
    assert captured["mode"] == "gainers"
    assert captured["limit"] == 20

    assert len(out) == 1
    item = out[0]
    assert isinstance(item, dc.HotStock)
    assert item.symbol == "000858"
    assert item.market == "CN"
    assert item.name == "五粮液"
    assert item.price == 150.0
    assert item.change_pct == 3.3
    assert item.turnover == 55555.0
    assert item.volume == 200.0


def test_fetch_market_map_uses_marketdata(monkeypatch):
    """fetch_market_map 走 marketdata 包的 market_map,透传 market/limit/proxy。"""
    from marketdata.types import MarketMapSnapshot as MdSnapshot
    from marketdata.types import MarketMapStock as MdStock

    captured: dict = {}

    class _MD:
        def market_map(self, *, market="CN", limit=400, proxy=None):
            captured.update(market=market, limit=limit, proxy=proxy)
            return MdSnapshot(
                stocks=[MdStock("601939", "CN", "建设银行", 10.0, 2.14, 2e11, "银行Ⅱ", 0.25)],
                total=5304, up=726, flat=73, down=4505, amount=1.5e12, as_of=1793950482,
            )

    monkeypatch.setattr(dc, "get_market_data", lambda: _MD())
    collector = dc.EastMoneyDiscoveryCollector(proxy="http://market-scan-proxy:1080")
    out = asyncio.run(collector.fetch_market_map(market="CN", limit=120))

    assert captured == {"market": "CN", "limit": 120, "proxy": "http://market-scan-proxy:1080"}
    assert isinstance(out, dc.MarketMapSnapshot)
    assert (out.total, out.up, out.flat, out.down) == (5304, 726, 73, 4505)
    assert out.amount == 1.5e12 and out.as_of == 1793950482
    stock = out.stocks[0]
    assert isinstance(stock, dc.MarketMapStock)
    assert (stock.symbol, stock.industry, stock.change_pct) == ("601939", "银行Ⅱ", 2.14)
    assert stock.change_amount == 0.25


def _route_module():
    import src.modules.market.api.discovery as discovery
    return discovery


def _local_snapshot():
    return dc.MarketMapSnapshot(
        stocks=[
            dc.MarketMapStock("600519", "CN", "贵州茅台", 1700.0, 1.5, 3e11, "白酒Ⅱ", 26.1),
            dc.MarketMapStock("000001", "CN", "平安银行", None, -0.5, 5e10, "银行Ⅱ"),
            dc.MarketMapStock("000002", "CN", "无市值", 1.0, 0.0, None, None),
        ],
        total=5304, up=726, flat=73, down=4505, amount=1.5e12, as_of=1793950482,
    )


def _patch_route(monkeypatch, snapshot=None, exc=None):
    discovery = _route_module()
    discovery._cache.clear()

    class _Collector:
        def __init__(self, *, proxy=None):
            self.proxy = proxy

        async def fetch_market_map(self, *, market="CN", limit=400):
            if exc is not None:
                raise exc
            return snapshot

    monkeypatch.setattr(discovery, "EastMoneyDiscoveryCollector", _Collector)
    monkeypatch.setattr(discovery, "_resolve_proxy", lambda: "")
    return discovery


def test_market_map_route_drops_missing_caps_and_passes_stats(monkeypatch):
    discovery = _patch_route(monkeypatch, snapshot=_local_snapshot())
    payload = asyncio.run(discovery.get_market_map(market="CN", limit=10))

    assert [it["symbol"] for it in payload["items"]] == ["600519", "000001"]
    assert payload["items"][0]["industry"] == "白酒Ⅱ"
    assert payload["items"][0]["change_amount"] == 26.1
    assert payload["stats"] == {
        "total": 5304, "up": 726, "flat": 73, "down": 4505,
        "amount": 1.5e12, "as_of": 1793950482,
    }
    # 第二次命中 90s 缓存,不再取数。
    again = asyncio.run(discovery.get_market_map(market="CN", limit=10))
    assert again is payload


def test_market_map_route_rejects_non_cn(monkeypatch):
    discovery = _patch_route(monkeypatch, snapshot=_local_snapshot())
    try:
        asyncio.run(discovery.get_market_map(market="HK", limit=10))
    except Exception as exc:  # HTTPException
        assert getattr(exc, "status_code", None) == 400
        assert getattr(exc, "detail", {}).get("code") == "market_map_market_unsupported"
    else:
        raise AssertionError("expected 400")


def test_market_map_route_maps_failures_to_503(monkeypatch):
    discovery = _patch_route(monkeypatch, exc=RuntimeError("boom"))
    try:
        asyncio.run(discovery.get_market_map(market="CN", limit=10))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 503
        assert getattr(exc, "detail", {}).get("code") == "market_map_unavailable"
    else:
        raise AssertionError("expected 503")


def test_market_map_route_empty_snapshot_is_503(monkeypatch):
    empty = dc.MarketMapSnapshot(stocks=[], total=0, up=0, flat=0, down=0, amount=None, as_of=None)
    discovery = _patch_route(monkeypatch, snapshot=empty)
    try:
        asyncio.run(discovery.get_market_map(market="CN", limit=10))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 503
    else:
        raise AssertionError("expected 503")


def test_market_map_sector_route_filters_full_snapshot(monkeypatch):
    """悬停板块接口:首次拉全量快照并按行业过滤,后续板块复用同一份全量缓存。"""
    discovery = _route_module()
    discovery._cache.clear()
    calls = []

    class _Collector:
        def __init__(self, *, proxy=None):
            pass

        async def fetch_market_map(self, *, market="CN", limit=400):
            calls.append(limit)
            return _local_snapshot()

    monkeypatch.setattr(discovery, "EastMoneyDiscoveryCollector", _Collector)
    monkeypatch.setattr(discovery, "_resolve_proxy", lambda: "")

    payload = asyncio.run(discovery.get_market_map_sector(industry="银行Ⅱ", market="CN"))
    assert [it["symbol"] for it in payload["items"]] == ["000001"]
    assert payload["count"] == 1
    assert calls == [6000]

    other = asyncio.run(discovery.get_market_map_sector(industry="白酒Ⅱ", market="CN"))
    assert [it["symbol"] for it in other["items"]] == ["600519"]
    assert calls == [6000]


def test_market_map_sector_route_requires_cn_and_industry(monkeypatch):
    discovery = _route_module()
    discovery._cache.clear()
    monkeypatch.setattr(discovery, "_resolve_proxy", lambda: "")

    for kwargs, code in (
        ({"industry": "银行Ⅱ", "market": "HK"}, "market_map_market_unsupported"),
        ({"industry": "  ", "market": "CN"}, "market_map_industry_required"),
    ):
        try:
            asyncio.run(discovery.get_market_map_sector(**kwargs))
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 400
            assert getattr(exc, "detail", {}).get("code") == code
        else:
            raise AssertionError(f"expected 400 for {kwargs}")
