"""美股 ETF/指数区间价格收益对比。

ETF 用 Nasdaq 公开历史行情（已按拆股调整，但不含分红再投资），指数用腾讯
美股指数日K（指数无拆股）。两者都只提供收盘价，因此本页签只展示价格收益，
不含分红。

Nasdaq 历史表的 EOD 行发布有延迟，指数日K 偶有坏值，因此再用腾讯美股实时
行情补齐/修复最后一个已收盘交易日；报价与历史基准对不上时不注入。

区间内以区间起点前最后一个收盘价为基准归一化为涨跌幅；各标的交易日不一致
时按日期并集对齐，缺失日不补值。
"""

from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from marketdata.vendors.kline import fetch_tencent_kline_raw
from marketdata.vendors.tencent import _fetch_lines

from src.modules.market.etf_share_service import _require_group
from src.modules.market.statistics_store import StatisticsStore
from src.platform.marketdata.collectors.market_http import market_get

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
_QUOTE_CACHE_TTL_S = 300
_VERIFIED_CLOSE_TTL_S = 7 * 24 * 3600
# 腾讯报价的昨收必须与历史收盘基本一致才视为同一口径，否则（如拆股复权差异）不注入。
_MATCH_TOLERANCE = 0.02
_MARKET_CLOSE = time(16, 0)
_ET = ZoneInfo("America/New_York")
_store = StatisticsStore(US_COMPARE_GROUP_ID, _CACHE_TTL_S)
_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class _Point:
    date: str
    close: float


@dataclass(frozen=True, slots=True)
class _Quote:
    date: str
    at: datetime
    close: float
    prev_close: float


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
        if math.isfinite(close) and close > 0:
            points.append(_Point(day, close))
    points.sort(key=lambda point: point.date)
    return points


def _fetch_nasdaq(code: str, start: date) -> list[_Point]:
    cached_asset = _store.get(f"asset:{code}", allow_stale=True)
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
            _store.set(f"asset:{code}", asset)
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
    with _lock:
        if not force:
            cached = _store.get(key)
            if cached is not None:
                return [_Point(**row) for row in cached]
        try:
            points = _fetch_symbol_points(code)
        except Exception as exc:
            logger.warning("[美股收益对比] %s 历史更新失败: %s", code, exc)
            points = []
        if points:
            # 拆股会重算整段复权历史，不能与旧快照拼接成两种价格口径。
            _store.set(key, [{"date": point.date, "close": point.close} for point in points])
            return points
        return [_Point(**row) for row in (_store.get(key, allow_stale=True) or [])]


def parse_tencent_quote(line: str) -> tuple[str, _Quote] | None:
    """解析一行腾讯行情：变量名给出腾讯符号，字段 3/4/30 给出收盘价、昨收与美东时间。"""
    name, _, value = line.strip().partition('="')
    parts = value.rstrip('";').split("~")
    if not name.startswith("v_us") or len(parts) < 31:
        return None
    try:
        at = datetime.strptime(parts[30], "%Y-%m-%d %H:%M:%S")
        close = float(parts[3])
        prev_close = float(parts[4])
    except ValueError:
        return None
    if not all(math.isfinite(price) and price > 0 for price in (close, prev_close)):
        return None
    return name.removeprefix("v_"), _Quote(at.date().isoformat(), at, close, prev_close)


def _quote_record(quote: _Quote) -> dict[str, Any]:
    return {
        "date": quote.date,
        "at": quote.at.isoformat(),
        "close": quote.close,
        "prev_close": quote.prev_close,
    }


def _stored_quote(key: str, *, allow_stale: bool = False) -> _Quote | None:
    record = _store.get(key, allow_stale=allow_stale)
    if record is None:
        return None
    return _Quote(record["date"], datetime.fromisoformat(record["at"]), record["close"], record["prev_close"])


def _latest_quotes(codes: tuple[str, ...], *, force: bool = False) -> dict[str, _Quote]:
    quotes: dict[str, _Quote] = {}
    symbols: dict[str, str] = {}
    for code in codes:
        cached = None if force else _stored_quote(f"quote:{code}")
        if cached is not None:
            quotes[code] = cached
        else:
            symbols[code] = _INDEX_SYMBOLS.get(code) or f"us{code}"
    if not symbols:
        return quotes
    by_symbol: dict[str, _Quote] = {}
    try:
        lines = _fetch_lines(list(dict.fromkeys(symbols.values())))
    except Exception as exc:
        logger.warning("[美股收益对比] 报价更新失败: %s", exc)
        lines = []
    for line in lines:
        parsed = parse_tencent_quote(line)
        if parsed is not None:
            by_symbol[parsed[0]] = parsed[1]
    for code, symbol in symbols.items():
        quote = by_symbol.get(symbol)
        if quote is not None:
            _store.set(f"quote:{code}", _quote_record(quote), _QUOTE_CACHE_TTL_S)
        else:
            quote = _stored_quote(f"quote:{code}", allow_stale=True)
        if quote is not None:
            quotes[code] = quote
    return quotes


def _matches(a: float, b: float) -> bool:
    return b > 0 and abs(a - b) / b <= _MATCH_TOLERANCE


def _is_closed_session(quote: _Quote, now_et: datetime) -> bool:
    # 不能把前一天的陈旧盘中报价当作收盘；提前休市日保守等待历史源。
    return quote.at.time() >= _MARKET_CLOSE and quote.at <= now_et.replace(tzinfo=None)


def _reconcile_latest(
    points: list[_Point], quote: _Quote | None, now_et: datetime
) -> tuple[list[_Point], bool]:
    """仅用已收盘且昨收口径一致的报价补齐或修正历史末日。"""
    if not points or quote is None or not _is_closed_session(quote, now_et):
        return points, False
    last = points[-1]
    if quote.date < last.date:
        return points, False
    same_day = quote.date == last.date
    if same_day and _matches(quote.close, last.close):
        return points, False
    anchor = (points[-2] if len(points) > 1 else None) if same_day else last
    if anchor is None or not _matches(quote.prev_close, anchor.close):
        return points, False
    if same_day:
        return [*points[:-1], _Point(last.date, quote.close)], True
    return [*points, _Point(quote.date, quote.close)], True


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
    quotes = _latest_quotes(symbols, force=force)
    now_et = datetime.now(_ET)
    per_symbol: dict[str, dict[str, float]] = {}
    bases: dict[str, str | None] = {}
    missing: list[str] = []
    backfilled = False
    for code in symbols:
        history = _symbol_points(code, force=force)
        quote = quotes.get(code)
        points, adjusted = _reconcile_latest(history, quote, now_et)
        verified_key = f"verified_close:{code}"
        verified = _stored_quote(verified_key, allow_stale=True)
        if adjusted:
            if verified != quote:
                _store.set(verified_key, _quote_record(quote), _VERIFIED_CLOSE_TTL_S)
        elif verified is not None:
            # 收盘补丁不过期删除；每次仍用历史昨收核验，防止复权变化后误用。
            points, adjusted = _reconcile_latest(history, verified, now_et)
        values, base_date = _normalized(points, start)
        if not values:
            missing.append(code)
            continue
        per_symbol[code] = values
        bases[code] = base_date
        backfilled = backfilled or adjusted

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
        "sources": _sources(per_symbol, backfilled),
    }


def _sources(
    per_symbol: dict[str, dict[str, float]], backfilled: bool
) -> list[dict[str, str]]:
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
    if backfilled:
        sources.append(
            {
                "id": "tencent_quote",
                "name": "腾讯美股收盘报价（补齐或校正历史末日）",
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
