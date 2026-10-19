"""持仓日内市值曲线(纯计算,不联网)。

口径:
- 时间轴 = 当日有分时数据的持仓的分钟戳并集(北京时间,A股/港股同区);
- 每个分钟点:市值 = Σ(持仓量 × 该分钟最新价 × 汇率),分时缺失的分钟向前填充;
- 当日休市/无分时的标的(如港股假期)按最新价恒定计入,不贡献日内波动,
  并在 ``coverage.flat`` 里列出,前端据此提示;
- 基准 = 昨收市值(Σ 量 × 昨收 × 汇率),用于算「日内变动」。

与「持仓回溯净值」不同,这条曲线只描述**当天**的盘中变化,不涉及历史重算。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

Series = dict[tuple[str, str], tuple[str | None, list[tuple[str, float]]]]


def _empty(reason: str, session_date: str | None = None) -> dict:
    return {
        "empty": True,
        "reason": reason,
        "session_date": session_date,
        "points": [],
        "summary": None,
    }


def _fmt_time(stamp: str) -> str:
    return f"{stamp[:2]}:{stamp[2:]}" if len(stamp) == 4 else stamp


def build_intraday_curve(
    holdings: list[dict],
    *,
    series: Series | None = None,
    prev_close: dict[tuple[str, str], float | None] | None = None,
    cash: float = 0.0,
    session_date: str | None = None,
) -> dict:
    """按当日分时构造持仓市值曲线。

    ``holdings`` 为统一持仓列表,需含 symbol / market / quantity / fx / market_value。
    ``series`` 为 {(market, symbol): (交易日, [(HHMM, 价格)])}。
    ``prev_close`` 为 {(market, symbol): 昨收},缺失时退回该标的首个分时价。
    """
    series = series or {}
    prev_close = prev_close or {}

    rows: list[dict] = []
    for h in holdings:
        try:
            qty = float(h.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        market = str(h.get("market") or "").upper()
        symbol = str(h.get("symbol") or "")
        if not symbol:
            continue
        fx = float(h.get("fx") or 1.0)
        mv = float(h.get("market_value") or 0.0)
        flat_price = (mv / (qty * fx)) if mv > 0 and qty * fx > 0 else None
        date, points = series.get((market, symbol), (None, []))
        rows.append(
            {
                "market": market,
                "symbol": symbol,
                "name": h.get("name") or symbol,
                "qty_fx": qty * fx,
                "date": date,
                "points": points,
                "flat_price": flat_price,
            }
        )

    if not rows:
        return _empty("no_holdings")

    # 交易日:显式传入优先,否则取各标的中最新的分时日期(周末/假期回落到上一交易日)。
    if not session_date:
        dates = [r["date"] for r in rows if r["date"] and r["points"]]
        session_date = max(dates) if dates else None

    intraday = [r for r in rows if r["date"] == session_date and len(r["points"]) >= 2]
    flat = [r for r in rows if r not in intraday]
    if not intraday:
        return _empty("no_intraday", session_date)

    timeline = sorted({stamp for r in intraday for stamp, _ in r["points"]})
    if len(timeline) < 2:
        return _empty("no_intraday", session_date)

    # 每个标的的价格序列按时间轴前向填充(开盘前用首价,停牌/缺分钟沿用上一价)。
    priced: list[tuple[float, list[float]]] = []
    for r in intraday:
        by_time = dict(r["points"])
        filled: list[float] = []
        last: float | None = None
        for stamp in timeline:
            price = by_time.get(stamp)
            if price is not None:
                last = price
            if last is None:
                last = r["points"][0][1]
            filled.append(last)
        priced.append((r["qty_fx"], filled))

    # 休市标的按最新价恒定计入,保证总市值与页面「持仓市值」一致。
    flat_value = 0.0
    for r in flat:
        price = r["flat_price"]
        if price is None:
            price = prev_close.get((r["market"], r["symbol"])) or 0.0
        flat_value += r["qty_fx"] * float(price or 0.0)

    points: list[dict] = []
    for i, stamp in enumerate(timeline):
        mv = flat_value + sum(qfx * filled[i] for qfx, filled in priced)
        points.append(
            {
                "time": _fmt_time(stamp),
                "market_value": round(mv, 2),
                "total_assets": round(mv + float(cash or 0.0), 2),
            }
        )

    # 昨收基准:有昨收用昨收,缺失用首个分时价(该标的日内变动即为 0)。
    baseline_mv = flat_value
    for r in intraday:
        base = prev_close.get((r["market"], r["symbol"]))
        if not base:
            base = r["points"][0][1]
        baseline_mv += r["qty_fx"] * float(base)

    current = points[-1]["market_value"]
    change = current - baseline_mv
    change_pct = (change / baseline_mv * 100) if baseline_mv > 0 else 0.0

    peak = max(points, key=lambda p: p["market_value"])
    trough = min(points, key=lambda p: p["market_value"])

    return {
        "empty": False,
        "session_date": session_date,
        "points": points,
        "summary": {
            "current_market_value": round(current, 2),
            "baseline_market_value": round(baseline_mv, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "high": round(peak["market_value"], 2),
            "high_time": peak["time"],
            "low": round(trough["market_value"], 2),
            "low_time": trough["time"],
            "cash": round(float(cash or 0.0), 2),
        },
        "coverage": {
            "positions": len(rows),
            "intraday": len(intraday),
            "flat": [f"{r['market']}:{r['symbol']}" for r in flat],
            "markets": sorted({r["market"] for r in intraday}),
        },
    }
