"""市场指数 API - 公共数据，无需认证"""
import asyncio
import logging
import time
import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.modules.market import fear_greed_service
from src.platform.marketdata.collectors.kline_collector import get_index_klines
from src.platform.marketdata.models import MarketCode
from src.platform.persistence.database import get_db
from src.web.errors import api_error

logger = logging.getLogger(__name__)
router = APIRouter()


def get_market_data():
    """惰性 import,避免包未装/循环 import 影响本模块加载。"""
    from src.platform.marketdata.marketdata_client import get_market_data as _g

    return _g()

# 主要市场指数配置
# response_symbol: 腾讯 API 返回的 symbol（用于匹配）
MARKET_INDICES = [
    # A股指数
    {"symbol": "000001", "name": "上证指数", "market": "CN", "tencent_symbol": "sh000001", "response_symbol": "000001"},
    {"symbol": "399001", "name": "深证成指", "market": "CN", "tencent_symbol": "sz399001", "response_symbol": "399001"},
    {"symbol": "399006", "name": "创业板指", "market": "CN", "tencent_symbol": "sz399006", "response_symbol": "399006"},
    {"symbol": "000688", "name": "科创50", "market": "CN", "tencent_symbol": "sh000688", "response_symbol": "000688"},
    # 港股指数
    {"symbol": "HSI", "name": "恒生指数", "market": "HK", "tencent_symbol": "hkHSI", "response_symbol": "HSI"},
    # 美股指数 (腾讯返回的 symbol 带点号前缀: .IXIC, .DJI)
    {"symbol": "IXIC", "name": "纳斯达克", "market": "US", "tencent_symbol": "usIXIC", "response_symbol": ".IXIC"},
    {"symbol": "DJI", "name": "道琼斯", "market": "US", "tencent_symbol": "usDJI", "response_symbol": ".DJI"},
]

# 指数响应内存缓存:60s(行情价格要新鲜)。
_INDICES_CACHE: dict[str, tuple[float, list[dict]]] = {}
_INDICES_CACHE_TTL_S = 60

# spark(近20日收盘)独立缓存:日线一天才变,30 分钟足够新鲜。
# 没有它,响应缓存每 60s 过期就要重付一轮 6×指数K线(部分环境东财先失败再腾讯兜底,
# 串行约 4s)——这曾是首页快车道最大的延迟来源。空结果也缓存(坏源别反复重拉)。
_SPARK_CACHE: dict[str, tuple[float, list[float]]] = {}
_SPARK_TTL_S = 1800


def clear_indices_cache() -> None:
    """清空指数响应/spark 缓存(测试隔离用)。"""
    _INDICES_CACHE.clear()
    _SPARK_CACHE.clear()


def _spark_for(idx: dict) -> list[float]:
    """近 20 日收盘价,供首页指数走势 sparkline 用(带 30min 独立缓存)。

    fail-soft:市场码非法/取数异常/无映射一律吞掉,返回空列表,绝不影响 quote 主体。
    """
    now = time.time()
    hit = _SPARK_CACHE.get(idx["symbol"])
    if hit and now - hit[0] < _SPARK_TTL_S:
        return hit[1]
    try:
        market_code = MarketCode(idx["market"])
        klines = get_index_klines(idx["symbol"], market_code, days=20)
        spark = [k.close for k in klines] if klines else []
    except Exception as e:
        logger.debug(f"指数 spark 获取失败 {idx['symbol']}: {e}")
        spark = []
    _SPARK_CACHE[idx["symbol"]] = (now, spark)
    return spark


@router.get("/indices")
async def get_market_indices():
    """获取主要市场指数（公共数据，无需认证）"""
    now = time.time()
    cached = _INDICES_CACHE.get("indices")
    if cached and now - cached[0] < _INDICES_CACHE_TTL_S:
        return cached[1]

    tencent_symbols = [idx["tencent_symbol"] for idx in MARKET_INDICES]

    try:
        # 外部行情是同步 I/O，不能占住事件循环，让首页其他 API 等它返回。
        quotes = await asyncio.to_thread(lambda: get_market_data().index_quotes(tencent_symbols))
    except Exception as e:
        logger.error(f"获取市场指数失败: {e}")
        return []

    # 构建 response_symbol -> quote 映射
    quote_map = {}
    for q in quotes:
        quote_map[q["symbol"]] = q

    # spark 并行取(缓存未过期时零成本;冷启动=最慢单个≈1s,而非 6 个串行累加)
    sparks = await asyncio.gather(
        *[asyncio.to_thread(_spark_for, idx) for idx in MARKET_INDICES],
        return_exceptions=True,
    )
    spark_map = {
        idx["symbol"]: (sp if isinstance(sp, list) else [])
        for idx, sp in zip(MARKET_INDICES, sparks)
    }

    result = []
    for idx in MARKET_INDICES:
        # 使用 response_symbol 匹配
        quote = quote_map.get(idx["response_symbol"])
        spark = spark_map.get(idx["symbol"], [])

        if quote:
            result.append({
                "symbol": idx["symbol"],
                "name": idx["name"],
                "market": idx["market"],
                "current_price": quote["current_price"],
                "change_pct": quote["change_pct"],
                "change_amount": quote["change_amount"],
                "prev_close": quote["prev_close"],
                "spark": spark,
            })
        else:
            # 即使没有行情也返回基本信息
            result.append({
                "symbol": idx["symbol"],
                "name": idx["name"],
                "market": idx["market"],
                "current_price": None,
                "change_pct": None,
                "change_amount": None,
                "prev_close": None,
                "spark": spark,
            })

    _INDICES_CACHE["indices"] = (now, result)
    return result


# ---------------------------------------------------------------------------
# 大宗商品 / 宏观行情（与指数同样式展示）
# 数据源统一为新浪财经：外盘期货 hf_、外盘指数（美元指数 DINIW）、国内期货主连 nf_。
# ---------------------------------------------------------------------------
COMMODITIES = [
    {"symbol": "USDX", "name": "美元指数", "source": "sina_global", "quote_symbol": "DINIW"},
    {"symbol": "CNH", "name": "离岸人民币", "source": "sina_global", "quote_symbol": "fx_susdcnh"},
    {"symbol": "OIL", "name": "布伦特原油", "source": "sina_hf", "quote_symbol": "hf_OIL"},
    {"symbol": "AHD", "name": "伦铝", "source": "sina_hf", "quote_symbol": "hf_AHD"},
    {"symbol": "AL0", "name": "AL0期货", "source": "sina_nf", "quote_symbol": "nf_AL0"},
    {"symbol": "XAU", "name": "伦金", "source": "sina_hf", "quote_symbol": "hf_XAU"},
    {"symbol": "CAD", "name": "伦铜", "source": "sina_hf", "quote_symbol": "hf_CAD"},
]
_COMM_CACHE: dict[str, tuple[float, list[dict]]] = {}
_COMM_CACHE_TTL_S = 60

_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": "Mozilla/5.0",
}


def _parse_sina_hf(text: str) -> dict[str, dict]:
    """解析新浪外盘期货 hf_（逗号格式；parts[0] 现价，parts[7] 昨结；涨跌由二者计算）。"""
    out: dict[str, dict] = {}
    for line in text.strip().splitlines():
        if '="' not in line:
            continue
        key, value = line.split('="', 1)
        code = key.replace("var hq_str_", "").strip()
        parts = value.rstrip('";').split(",")
        if len(parts) < 14:
            continue
        try:
            last = float(parts[0])
            prev_settle = float(parts[7])
        except ValueError:
            continue
        change = last - prev_settle
        change_pct = (change / prev_settle * 100) if prev_settle else None
        out[code] = {
            "current_price": last,
            "change_amount": change,
            "change_pct": change_pct,
        }
    return out


def _parse_sina_global(text: str) -> dict[str, dict]:
    """解析新浪外盘指数（如美元指数 DINIW）：parts[1] 现价，parts[3] 昨收。"""
    out: dict[str, dict] = {}
    for line in text.strip().splitlines():
        if '="' not in line:
            continue
        key, value = line.split('="', 1)
        code = key.replace("var hq_str_", "").strip()
        parts = value.rstrip('";').split(",")
        if len(parts) < 10:
            continue
        try:
            last = float(parts[1])
            prev_close = float(parts[3])
        except ValueError:
            continue
        change = last - prev_close
        change_pct = (change / prev_close * 100) if prev_close else None
        out[code] = {
            "current_price": last,
            "change_amount": change,
            "change_pct": change_pct,
        }
    return out


def _parse_sina_nf(text: str) -> dict[str, dict]:
    """解析新浪国内期货主连（parts[8] 最新价，parts[10] 昨结算）。"""
    out: dict[str, dict] = {}
    for line in text.strip().splitlines():
        if '="' not in line:
            continue
        key, value = line.split('="', 1)
        code = key.replace("var hq_str_", "").strip()
        parts = value.rstrip('";').split(",")
        if len(parts) < 11:
            continue
        try:
            last = float(parts[8])
            prev_settle = float(parts[10])
        except ValueError:
            continue
        change = last - prev_settle
        change_pct = (change / prev_settle * 100) if prev_settle else None
        out[code] = {
            "current_price": last,
            "change_amount": change,
            "change_pct": change_pct,
        }
    return out


def _build_commodities() -> list[dict]:
    groups: dict[str, list[str]] = {}
    for item in COMMODITIES:
        groups.setdefault(item["source"], []).append(item["quote_symbol"])

    parsers = {
        "sina_hf": _parse_sina_hf,
        "sina_global": _parse_sina_global,
        "sina_nf": _parse_sina_nf,
    }
    fetched: dict[str, dict[str, dict]] = {}
    for source, symbols in groups.items():
        try:
            url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
            resp = httpx.get(url, timeout=10, headers=_SINA_HEADERS)
            fetched[source] = parsers[source](resp.content.decode("gbk", "ignore"))
        except Exception as e:  # 单个来源失败不影响其余品种
            logger.warning(f"获取商品行情失败 {source}: {e}")

    result = []
    for item in COMMODITIES:
        quote = fetched.get(item["source"], {}).get(item["quote_symbol"])
        result.append({
            "symbol": item["symbol"],
            "name": item["name"],
            "current_price": quote["current_price"] if quote else None,
            "change_amount": round(quote["change_amount"], 4) if quote else None,
            "change_pct": round(quote["change_pct"], 2) if quote and quote["change_pct"] is not None else None,
        })
    return result


@router.get("/commodities")
async def get_commodities():
    """获取大宗商品/宏观行情（公共数据，无需认证）。"""
    now = time.time()
    cached = _COMM_CACHE.get("commodities")
    if cached and now - cached[0] < _COMM_CACHE_TTL_S:
        return cached[1]
    result = await asyncio.to_thread(_build_commodities)
    _COMM_CACHE["commodities"] = (now, result)
    return result


@router.get("/fear-greed")
async def get_fear_greed(
    days: int = Query(default=30, ge=5, le=365),
    db: Session = Depends(get_db),
):
    """A 股恐贪指数（百分位，0–100）；数据源不可用时回退本地缓存。"""
    try:
        return await asyncio.to_thread(fear_greed_service.get_payload, db, days)
    except fear_greed_service.FearGreedUnavailable as exc:
        raise api_error(503, "fear_greed_unavailable", "A股恐贪指数暂不可用") from exc
