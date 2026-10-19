"""分时(分钟级)行情采集器 —— 腾讯分时接口。

只取「最近一个交易日」的完整分时序列,供持仓页日内市值曲线使用:
- A股 / 港股 / 美股 统一走 ``web.ifzq.gtimg.cn/appstock/app/minute/query``
- 返回该序列所属交易日(YYYY-MM-DD)与 [(HHMM, price)],未开盘/休市时返回上一交易日

与日K采集器一致,带 host 节流 + TTL 缓存:曲线每分钟刷新一次,15 只持仓
会集中打同一域名,缓存可把重复请求压掉。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from src.platform.marketdata.cn_symbol import get_cn_prefix
from src.platform.marketdata.collectors.market_http import TTLCache, market_get
from src.platform.marketdata.models import MARKETS, MarketCode

logger = logging.getLogger(__name__)

_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
_HOST_KEY = "ifzq.gtimg.cn"

# 分时数据每分钟才变一次,30s 缓存足以覆盖同一页面的并发刷新。
_CACHE = TTLCache(default_ttl_sec=30.0, max_size=1024)


def clear_intraday_cache() -> None:
    """清空分时缓存(测试隔离用)。"""
    _CACHE.clear()


def tencent_code(symbol: str, market: str) -> str:
    """组腾讯分时代码:A股 sh/sz/bj 前缀、港股 hk+5位、美股 us+代码。"""
    sym = (symbol or "").strip().upper()
    mkt = (market or "").upper()
    if mkt == MarketCode.CN.value:
        return f"{get_cn_prefix(sym)}{sym}"
    if mkt == MarketCode.HK.value:
        return f"hk{sym.zfill(5)}"
    if mkt == MarketCode.US.value:
        return f"us{sym}"
    return sym.lower()


def _parse_date(raw: str | None) -> str | None:
    """腾讯返回 20261019,统一成 2026-10-19。"""
    if not raw or len(raw) != 8 or not raw.isdigit():
        return None
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"


def parse_minute_payload(payload: dict, code: str) -> tuple[str | None, list[tuple[str, float]]]:
    """从腾讯分时 JSON 里取出 (交易日, [(HHMM, 价格)])。

    行格式:``0930 1322.50 131 17324750.50``(时间 价格 成交量 成交额)。
    同一分钟重复出现时保留最后一条(收盘集合竞价会追加 1500)。
    """
    try:
        node = (payload.get("data") or {}).get(code) or {}
        inner = node.get("data") or {}
        rows = inner.get("data") or []
    except AttributeError:
        return None, []

    date = _parse_date(str(inner.get("date") or ""))
    prices: dict[str, float] = {}
    order: list[str] = []
    for row in rows:
        if not isinstance(row, str):
            continue
        parts = row.split()
        if len(parts) < 2:
            continue
        stamp = parts[0].strip()
        try:
            price = float(parts[1])
        except (TypeError, ValueError):
            continue
        if price <= 0 or len(stamp) != 4 or not stamp.isdigit():
            continue
        if stamp not in prices:
            order.append(stamp)
        prices[stamp] = price

    return date, [(s, prices[s]) for s in sorted(order)]


def _session_bounds(market: str) -> list[tuple[str, str]]:
    """取该市场的连续竞价时段(HHMM),用于裁掉集合竞价/盘后固定价格等尾行。"""
    try:
        definition = MARKETS.get(MarketCode((market or "").upper()))
    except ValueError:
        return []
    if not definition:
        return []
    return [
        (s.start.strftime("%H%M"), s.end.strftime("%H%M"))
        for s in definition.sessions
    ]


def _trim_to_session(
    points: list[tuple[str, float]], market: str
) -> list[tuple[str, float]]:
    """只保留交易时段内的分钟戳;收盘后的最终成交价回填到最后一个时段内分钟。

    腾讯分时会多给盘后行(A股 1501-1530、港股 1601-1608 收盘竞价),
    直接画到曲线上会出现一段无意义的平尾,但收盘竞价价格才是真收盘价,
    所以保留时段内行、把最后一行价格覆盖为序列最终价。
    """
    bounds = _session_bounds(market)
    if not bounds or not points:
        return points

    def inside(stamp: str) -> bool:
        return any(start <= stamp <= end for start, end in bounds)

    kept = [(s, p) for s, p in points if inside(s)]
    if not kept:
        return []
    last_stamp, last_price = points[-1]
    if last_stamp > kept[-1][0]:
        kept[-1] = (kept[-1][0], last_price)
    return kept


def get_intraday(symbol: str, market: str) -> tuple[str | None, list[tuple[str, float]]]:
    """取单只标的最近一个交易日的分时序列;失败返回 (None, []),不抛异常。"""
    code = tencent_code(symbol, market)
    cache_key = f"minute:{code}"
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    payload = market_get(
        _URL,
        host_key=_HOST_KEY,
        params={"code": code},
        parse="json",
        min_interval_s=0.12,
        timeout=8.0,
        retries=1,
        symbol=symbol,
        log_label="腾讯分时",
    )
    if not isinstance(payload, dict):
        return None, []

    date, points = parse_minute_payload(payload, code)
    points = _trim_to_session(points, market)
    if not points:
        logger.debug("分时无数据: %s/%s (%s)", market, symbol, code)
        return date, []

    result = (date, points)
    _CACHE.set(cache_key, result)
    return result


def get_intraday_many(
    pairs: list[tuple[str, str]], max_workers: int = 4
) -> dict[tuple[str, str], tuple[str | None, list[tuple[str, float]]]]:
    """批量取分时(带并发上限,避免把同一域名打爆)。pairs 为 [(market, symbol)]。"""
    unique = [(m.upper(), s) for m, s in pairs if s]
    if not unique:
        return {}

    def _one(item: tuple[str, str]):
        market, symbol = item
        try:
            return item, get_intraday(symbol, market)
        except Exception:
            logger.warning("分时取数异常: %s/%s", market, symbol, exc_info=True)
            return item, (None, [])

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(unique)))) as pool:
        return dict(pool.map(_one, unique))
