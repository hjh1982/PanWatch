"""美股 ETF/指数区间价格收益对比。

ETF 用 Nasdaq 公开历史行情（已按拆股调整，但不含分红再投资），指数用腾讯
美股指数日K（指数无拆股）。两者都只提供收盘价，因此本页签只展示价格收益，
不含分红。

区间内以区间起点前最后一个收盘价为基准归一化为涨跌幅；各标的交易日不一致
时按日期并集对齐，缺失日不补值。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from marketdata.vendors.kline import fetch_tencent_kline_raw

from src.modules.market.etf_share_service import _require_group
from src.platform.marketdata.collectors.market_http import TTLCache, market_get

logger = logging.getLogger(__name__)

US_COMPARE_GROUP_ID = "usCompare"

DEFAULT_SYMBOLS: tuple[str, ...] = ("SCHD", "SP500", "DGRO", "QQQ", "VYM", "SMH")
MAX_SYMBOLS = 8
MAX_BARS = 800
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}$")

# 指数与 ETF 的腾讯符号规则不同，只有显式映射的指数才允许走腾讯。
_INDEX_SYMBOLS = {
    "SP500": "us.INX",
    "SPX": "us.INX",
    "NDX": "us.NDX",
    "IXIC": "us.IXIC",
    "COMP": "us.IXIC",
    "DJI": "us.DJI",
}
# 常用美股 ETF/指数/个股的中文名；未收录的代码直接显示代码本身。
_SYMBOL_NAMES = {
    "SCHD": "嘉信美国红利股ETF",
    "SP500": "标普500指数",
    "SPX": "标普500指数",
    "DGRO": "iShares核心红利增长ETF",
    "QQQ": "纳指100ETF",
    "VYM": "先锋高股息ETF",
    "SPY": "标普500ETF",
    "VOO": "先锋标普500ETF",
    "IVV": "iShares核心标普500ETF",
    "VTI": "先锋全市场ETF",
    "IWM": "iShares罗素2000ETF",
    "DIA": "道琼斯工业平均ETF",
    "VIG": "先锋股息增长ETF",
    "SMH": "VanEck半导体ETF",
    "SOXX": "iShares半导体ETF",
    "XSD": "SPDR标普半导体ETF",
    "NDX": "纳斯达克100指数",
    "IXIC": "纳斯达克综合指数",
    "COMP": "纳斯达克综合指数",
    "DJI": "道琼斯工业指数",
    "AAPL": "苹果",
    "MSFT": "微软",
    "NVDA": "英伟达",
    "GOOGL": "谷歌A",
    "AMZN": "亚马逊",
    "META": "Meta",
    "TSLA": "特斯拉",
}
_RANGES = {
    "ytd": "年初至今",
    "1y": "近 1 年",
    "3y": "近 3 年",
}
_BUFFER_DAYS = 10  # 多取几天，保证区间起点前有基准收盘价

_NASDAQ_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
_NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nasdaq.com/",
}
_TENCENT_QUOTE_PAGE = "https://gu.qq.com/"

_CACHE_TTL_S = 1800
_cache = TTLCache(default_ttl_sec=_CACHE_TTL_S)
_lock = threading.Lock()
_asset_cache: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class _Point:
    date: str
    close: float


def parse_symbols(raw: str) -> tuple[str, ...]:
    codes: list[str] = []
    for part in str(raw or "").split(","):
        code = part.strip().upper()
        if not code:
            continue
        if not _SYMBOL_RE.fullmatch(code):
            raise ValueError(f"标的代码无效：{code}")
        if code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError("请至少选择一个标的")
    if len(codes) > MAX_SYMBOLS:
        raise ValueError(f"最多对比 {MAX_SYMBOLS} 个标的")
    return tuple(codes)


def normalize_range(raw: str) -> str:
    key = str(raw or "").strip().lower()
    if key not in _RANGES:
        raise ValueError("区间仅支持 ytd / 1y / 3y")
    return key


def _range_start(key: str, today: date) -> date:
    if key == "ytd":
        return date(today.year, 1, 1)
    if key == "1y":
        return today - timedelta(days=365)
    return today - timedelta(days=365 * 3)


def parse_nasdaq_history(payload: Any) -> list[_Point]:
    rows = (
        ((payload or {}).get("data") or {})
        .get("tradesTable", {})
        .get("rows")
        or []
    )
    points: list[_Point] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = datetime.strptime(
                str(row.get("date") or ""), "%m/%d/%Y"
            ).date().isoformat()
            close = float(
                str(row.get("close") or "").replace("$", "").replace(",", "")
            )
        except (TypeError, ValueError):
            continue
        if close > 0:
            points.append(_Point(day, close))
    points.sort(key=lambda point: point.date)
    return points


def _fetch_nasdaq(code: str, start: date) -> list[_Point]:
    cached_asset = _asset_cache.get(code)
    assets = [cached_asset] if cached_asset else ["etf", "stocks"]
    for asset in assets:
        payload = market_get(
            _NASDAQ_URL.format(symbol=code),
            host_key="api.nasdaq.com",
            params={
                "assetclass": asset,
                "fromdate": (start - timedelta(days=_BUFFER_DAYS)).isoformat(),
                "todate": date.today().isoformat(),
                "limit": "9999",
            },
            headers=_NASDAQ_HEADERS,
            timeout=20.0,
            total_timeout=40.0,
            parse="json",
            symbol=code,
            log_label="Nasdaq历史",
        )
        points = parse_nasdaq_history(payload)
        if points:
            _asset_cache[code] = asset
            return points
    return []


def _fetch_symbol_points(code: str) -> list[_Point]:
    index_symbol = _INDEX_SYMBOLS.get(code)
    if index_symbol is not None:
        return [
            _Point(str(bar.date), float(bar.close))
            for bar in fetch_tencent_kline_raw(index_symbol, MAX_BARS)
            if float(bar.close or 0) > 0
        ]
    start = date.today() - timedelta(days=365 * 3)
    return _fetch_nasdaq(code, start)


def _symbol_points(code: str, *, force: bool) -> list[_Point]:
    key = f"points:{code}"
    if not force:
        cached = _cache.get(key)
        if isinstance(cached, list):
            return cached
    with _lock:
        if not force:
            cached = _cache.get(key)
            if isinstance(cached, list):
                return cached
        points = _fetch_symbol_points(code)
        if points:
            _cache.set(key, points)
        return points


def _normalized(
    points: list[_Point], start: date
) -> tuple[dict[str, float], str | None]:
    """以区间起点前最后一个收盘价为基准，返回 {日期: 涨跌幅%} 与基准日。"""
    ordered = sorted(points, key=lambda point: point.date)
    if not ordered:
        return {}, None
    base = None
    first_include = 0
    for index, point in enumerate(ordered):
        if point.date <= start.isoformat():
            base = point
            first_include = index + 1
        else:
            break
    if base is None:
        base = ordered[0]
        first_include = 0
    if base.close <= 0:
        return {}, None
    values: dict[str, float] = {}
    for point in ordered[first_include:]:
        if point.close <= 0:
            continue
        values[point.date] = round((point.close / base.close - 1) * 100, 4)
    return values, base.date


def build_payload(
    symbols: tuple[str, ...],
    range_key: str,
    *,
    today: date | None = None,
    force: bool = False,
) -> dict[str, Any]:
    group = _require_group(US_COMPARE_GROUP_ID)
    start = _range_start(range_key, today or date.today())
    per_symbol: dict[str, dict[str, float]] = {}
    bases: dict[str, str | None] = {}
    missing: list[str] = []
    for code in symbols:
        values, base_date = _normalized(_symbol_points(code, force=force), start)
        if not values:
            missing.append(code)
            continue
        per_symbol[code] = values
        bases[code] = base_date

    dates = sorted({day for values in per_symbol.values() for day in values})
    series = {
        code: [per_symbol[code].get(day) for day in dates]
        for code in symbols
        if code in per_symbol
    }
    items: list[dict[str, Any]] = []
    for code in symbols:
        if code not in per_symbol:
            continue
        values = per_symbol[code]
        latest_date = max(values)
        items.append(
            {
                "code": code,
                "label": _SYMBOL_NAMES.get(code, code),
                "start_date": bases.get(code),
                "latest_date": latest_date,
                "latest": values[latest_date],
                "points": len(values),
            }
        )
    covered = sum(
        1
        for position in range(len(dates))
        if all(values[position] is not None for values in series.values())
    )
    as_of = max((item["latest_date"] for item in items), default=None)
    complete = bool(items) and all(
        values[-1] is not None for values in series.values()
    )
    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "美股 ETF/指数区间价格收益（不含分红）",
        "range": range_key,
        "range_label": _RANGES[range_key],
        "metric": {"id": "price_return", "label": "价格收益（不含分红）"},
        "as_of": as_of,
        "dates": dates,
        "symbols": [item["code"] for item in items],
        "missing": missing,
        "items": items,
        "series": series,
        "coverage": {
            "trading_days": covered,
            "target": len(dates),
            "complete": complete,
        },
        "sources": _sources(per_symbol),
    }


def _sources(per_symbol: dict[str, dict[str, float]]) -> list[dict[str, str]]:
    codes = set(per_symbol)
    sources: list[dict[str, str]] = []
    if codes - set(_INDEX_SYMBOLS):
        sources.append(
            {
                "id": "nasdaq",
                "name": "Nasdaq 历史行情（已按拆股调整）",
                "url": "https://www.nasdaq.com/market-activity/etf",
                "cadence": "日度",
            }
        )
    if codes & set(_INDEX_SYMBOLS):
        sources.append(
            {
                "id": "tencent",
                "name": "腾讯美股指数日K",
                "url": _TENCENT_QUOTE_PAGE,
                "cadence": "日度",
            }
        )
    return sources


def get_compare_payload(
    symbols_raw: str,
    range_raw: str,
    *,
    today: date | None = None,
    force: bool = False,
) -> dict[str, Any]:
    symbols = parse_symbols(symbols_raw)
    range_key = normalize_range(range_raw)
    return build_payload(symbols, range_key, today=today, force=force)
