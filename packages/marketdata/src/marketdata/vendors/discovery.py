"""发现(东财热门榜)vendor:单源、市场级、非 symbol 模型。

移植自 PanWatch src/collectors/discovery_collector.py 的
fetch_hot_stocks(L55-105)/fetch_hot_boards(L107-149)/fetch_board_stocks(L151-196)/
_get_json(L198-248):fid/fields/fs/params 计算与 f-code 字段映射(f12/f14/f2/f3/f4/f5/f6)
逐一照搬。原实现是 async(httpx.AsyncClient);此处改为同步 market_get。

不继承 marketdata.vendors.base.Vendor —— discovery 是市场级、单源、非 symbol 的取数,
硬套 symbol-based 失败转移 Engine 是设计错配,故不进 Engine/不进 DataSource taxonomy。
vendor 内不做缓存(TTL 缓存留宿主 collector)。
"""
from __future__ import annotations

from typing import Any

from marketdata.http import market_get
from marketdata.types import HotBoard, HotStock, MarketMapSnapshot, MarketMapStock

# push2 occasionally accepts the TCP connection and closes it before sending
# headers in desktop/NAS proxy environments. push2delay exposes the same clist
# contract and is reachable both directly and through the configured proxy.
_STOCKS_API = "https://push2delay.eastmoney.com/api/qt/clist/get"
_BOARDS_API = "https://push2delay.eastmoney.com/api/qt/clist/get"
_HOST_KEY = "push2.eastmoney.com"

# 东财「大盘星图」:baseinfo 给全量代码/东财二级行业,quotedata 给全量行情。
_HOTMAP_API = "https://quote.eastmoney.com/stockhotmap/api"
_HOTMAP_HOST_KEY = "quote.eastmoney.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def _normalize_diff(data: dict | None) -> list[dict]:
    """东财 clist 的 diff 字段可能是 list,也可能是 dict(按 index 为 key)。统一成 list。"""
    diff = ((data or {}).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        return list(diff.values())
    return diff


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hotmap_stock_meta(rows: list[str]) -> dict[tuple[str, str], tuple[str, int]]:
    """baseinfo: bk1|bk2|bk3|name|market|code|labels。"""
    meta: dict[tuple[str, str], tuple[str, int]] = {}
    for row in rows:
        parts = str(row or "").split("|")
        if len(parts) < 7:
            continue
        try:
            bk2_index = int(parts[1])
        except (TypeError, ValueError):
            continue
        key = (parts[4].strip(), parts[5].strip())
        if key[0] and key[1]:
            meta[key] = (parts[3].strip(), bk2_index)
    return meta


def _hotmap_industry_names(rows: list[str]) -> dict[int, str]:
    """bk2: name|market|code|bksort,下标即 baseinfo 的 bk2_index。"""
    names: dict[int, str] = {}
    for index, row in enumerate(rows):
        name = str(row or "").split("|", 1)[0].strip()
        if name:
            names[index] = name
    return names


class DiscoveryVendor:
    """东财热门榜(股票/板块/板块成分)。不继承 Vendor(市场级、非 symbol)。"""

    def hot_stocks(
        self,
        *,
        market: str = "CN",
        mode: str = "turnover",
        limit: int = 20,
        proxy: str | None = None,
    ) -> list[HotStock]:
        market = (market or "CN").upper()

        fid = "f6" if mode == "turnover" else "f3"
        fields = "f12,f14,f2,f3,f6,f5"
        if market == "CN":
            fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"  # A-share
        elif market == "HK":
            fs = "m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2"  # HK
        elif market == "US":
            fs = "m:105,m:106,m:107"  # US
        else:
            return []

        params = {
            "pn": 1,
            "pz": max(1, min(int(limit), 100)),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": fs,
            "fields": fields,
        }

        data = self._get_json(_STOCKS_API, params=params, proxy=proxy)
        result: list[HotStock] = []
        for it in _normalize_diff(data):
            try:
                result.append(
                    HotStock(
                        symbol=str(it.get("f12") or "").strip(),
                        market=market,
                        name=str(it.get("f14") or "").strip(),
                        price=it.get("f2"),
                        change_pct=it.get("f3"),
                        turnover=it.get("f6"),
                        volume=it.get("f5"),
                    )
                )
            except Exception:
                continue
        return result

    def hot_boards(
        self,
        *,
        market: str = "CN",
        mode: str = "gainers",
        limit: int = 12,
        proxy: str | None = None,
    ) -> list[HotBoard]:
        if market != "CN":
            return []

        fid = "f3" if mode in ("gainers", "hot") else "f6"
        fields = "f12,f14,f2,f3,f4,f6"
        fs = "m:90+t:2"  # industry boards

        params = {
            "pn": 1,
            "pz": max(1, min(int(limit), 100)),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": fs,
            "fields": fields,
        }

        data = self._get_json(_BOARDS_API, params=params, proxy=proxy)
        result: list[HotBoard] = []
        for it in _normalize_diff(data):
            try:
                result.append(
                    HotBoard(
                        code=str(it.get("f12") or "").strip(),
                        name=str(it.get("f14") or "").strip(),
                        change_pct=it.get("f3"),
                        change_amount=it.get("f4"),
                        turnover=it.get("f6"),
                    )
                )
            except Exception:
                continue
        return result

    def board_stocks(
        self,
        *,
        board_code: str,
        mode: str = "gainers",
        limit: int = 20,
        proxy: str | None = None,
    ) -> list[HotStock]:
        code = (board_code or "").strip()
        if not code:
            return []

        fid = "f3" if mode in ("gainers", "hot") else "f6"
        fields = "f12,f14,f2,f3,f6,f5"
        fs = f"b:{code}"

        params = {
            "pn": 1,
            "pz": max(1, min(int(limit), 100)),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": fid,
            "fs": fs,
            "fields": fields,
        }

        data = self._get_json(_STOCKS_API, params=params, proxy=proxy)
        result: list[HotStock] = []
        for it in _normalize_diff(data):
            try:
                result.append(
                    HotStock(
                        symbol=str(it.get("f12") or "").strip(),
                        market="CN",
                        name=str(it.get("f14") or "").strip(),
                        price=it.get("f2"),
                        change_pct=it.get("f3"),
                        turnover=it.get("f6"),
                        volume=it.get("f5"),
                    )
                )
            except Exception:
                continue
        return result

    def market_map(
        self,
        *,
        market: str = "CN",
        limit: int = 400,
        proxy: str | None = None,
    ) -> MarketMapSnapshot:
        """东财大盘星图:全市场快照 + 总市值前 N 的个股(东财二级行业分组)。"""
        market = (market or "CN").upper()
        if market != "CN":
            return MarketMapSnapshot(stocks=[], total=0, up=0, flat=0, down=0)

        base = self._get_json(
            f"{_HOTMAP_API}/getquotebasedata?hash=",
            params=None,
            proxy=proxy,
            host_key=_HOTMAP_HOST_KEY,
            timeout=20,
        )
        quotes = self._get_json(
            f"{_HOTMAP_API}/getquotedata?quotedata_hash=",
            params=None,
            proxy=proxy,
            host_key=_HOTMAP_HOST_KEY,
            timeout=20,
        )
        if not base.get("baseinfo") or not quotes.get("data"):
            raise RuntimeError("东财大盘星图返回空数据")

        meta = _hotmap_stock_meta(base.get("baseinfo") or [])
        industries = _hotmap_industry_names(base.get("bk2") or [])

        stocks: list[MarketMapStock] = []
        up = flat = down = 0
        amount = 0.0
        for row in quotes.get("data") or []:
            if not isinstance(row, str) or "|" not in row:
                continue  # 个别股票是密文行,跳过
            parts = row.split("|")
            if len(parts) < 15:
                continue
            info = meta.get((parts[0], parts[1]))
            change = _to_float(parts[3])
            if not info or change is None:
                continue

            change_pct = change / 100
            if change_pct > 0.005:
                up += 1
            elif change_pct < -0.005:
                down += 1
            else:
                flat += 1

            turnover = _to_float(parts[10])
            if turnover:
                amount += turnover

            price = _to_float(parts[12])
            cap = _to_float(parts[14])
            delta = _to_float(parts[2])
            name, bk2_index = info
            stocks.append(
                MarketMapStock(
                    symbol=parts[1],
                    market=market,
                    name=name,
                    price=price / 100 if price is not None else None,
                    change_pct=change_pct,
                    total_market_value=cap * 1_000_000 if cap is not None else None,
                    industry=industries.get(bk2_index),
                    change_amount=delta / 100 if delta is not None else None,
                )
            )

        stocks.sort(key=lambda item: item.total_market_value or 0, reverse=True)
        keep = max(1, min(int(limit), 6000))
        as_of = quotes.get("quotetime")
        return MarketMapSnapshot(
            stocks=stocks[:keep],
            total=up + flat + down,
            up=up,
            flat=flat,
            down=down,
            amount=amount or None,
            as_of=int(as_of) if isinstance(as_of, (int, float)) else None,
        )

    def _get_json(
        self,
        url: str,
        *,
        params: dict | None,
        proxy: str | None,
        host_key: str = _HOST_KEY,
        timeout: float = 10,
    ) -> dict[str, Any]:
        data = market_get(
            url,
            host_key=host_key,
            params=params,
            headers=_HEADERS,
            proxy=proxy,
            verify=False,
            parse="json",
            retries=1,
            timeout=timeout,
            min_interval_s=0.0,
            log_label="发现榜单",
        )
        return data or {}
