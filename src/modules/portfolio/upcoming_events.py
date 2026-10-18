"""持仓未来事件日历：分红登记/除息、限售解禁、财报法定截止日。

数据源：东方财富数据中心 datacenter-web；财报截止日为固定规则。
事件统一为 {date, type, symbol, name, title, detail}。
"""

import logging
from datetime import date, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

DC_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/",
}


def _dc(
    report_name: str,
    filter_str: str = "",
    sort_columns: str = "",
    sort_types: int = -1,
    page_size: int = 100,
) -> list[dict]:
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "pageSize": page_size,
        "pageNumber": 1,
    }
    if filter_str:
        params["filter"] = filter_str
    if sort_columns:
        params["sortColumns"] = sort_columns
        params["sortTypes"] = sort_types
    try:
        resp = httpx.get(DC_API, params=params, headers=HEADERS, timeout=10)
        result = (resp.json().get("result") or {})
        return result.get("data") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("东财数据中心获取失败 %s: %s", report_name, e)
        return []


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _in_filter(symbols: list[str]) -> str:
    codes = ",".join(f'"{s}"' for s in symbols)
    return f"(SECURITY_CODE in ({codes}))"


def _event(ev_date: date, ev_type: str, symbol: str, name: str,
           title: str, detail: str = "") -> dict:
    return {
        "date": ev_date.isoformat(),
        "type": ev_type,
        "symbol": symbol,
        "name": name,
        "title": title,
        "detail": detail,
    }


def dividend_events(symbols: list[str], name_map: dict[str, str],
                    today: date) -> list[dict]:
    """分红：股权登记日、除权除息日（仅未来日期）。"""
    rows = _dc("RPT_SHAREBONUS_DET", _in_filter(symbols),
                sort_columns="NOTICE_DATE")
    events: list[dict] = []
    for row in rows:
        symbol = row.get("SECURITY_CODE") or ""
        name = row.get("SECURITY_NAME_ABBR") or name_map.get(symbol, "")
        plan = row.get("IMPL_PLAN_PROFILE") or ""
        record_date = _parse_date(row.get("EQUITY_RECORD_DATE"))
        ex_date = _parse_date(row.get("EX_DIVIDEND_DATE"))
        if record_date and record_date >= today:
            events.append(_event(record_date, "dividend_record", symbol, name,
                                 "股权登记日", plan))
        if ex_date and ex_date >= today:
            events.append(_event(ex_date, "dividend_ex", symbol, name,
                                 "除权除息日", plan))
    return events


def lift_events(symbols: list[str], name_map: dict[str, str],
                today: date) -> list[dict]:
    """限售解禁：未来 FREE_DATE。"""
    rows = _dc("RPT_LIFT_STAGE", _in_filter(symbols),
               sort_columns="FREE_DATE")
    events: list[dict] = []
    max_date = today + timedelta(days=3 * 365)
    for row in rows:
        free_date = _parse_date(row.get("FREE_DATE"))
        if not free_date or free_date < today or free_date > max_date:
            continue
        symbol = row.get("SECURITY_CODE") or ""
        name = row.get("SECURITY_NAME_ABBR") or name_map.get(symbol, "")
        shares = row.get("FREE_SHARES")
        cap = row.get("LIFT_MARKET_CAP")
        kind = row.get("FREE_SHARES_TYPE") or ""
        detail = "".join(
            [
                f"{shares}万股" if shares else "",
                f" · 市值约{cap}万元" if cap else "",
                f" · {kind}" if kind else "",
            ]
        )
        events.append(_event(free_date, "lift", symbol, name, "限售解禁", detail))
    return events


def disclosure_events(meta: list[tuple[str, str, str]], today: date) -> list[dict]:
    """个股财报预约披露日（东财 F10 操盘必读-大事提醒，逐只请求）。

    meta: [(symbol, name, secucode)]；secucode 形如 SH600150 / SZ000807。
    """
    from concurrent.futures import ThreadPoolExecutor

    OPERATIONS_API = "https://emweb.eastmoney.com/PC_HSF10/OperationsRequired/PageAjax"

    def fetch_one(item: tuple[str, str, str]) -> list[dict]:
        symbol, name, secucode = item
        try:
            resp = httpx.get(
                OPERATIONS_API,
                params={"code": secucode},
                headers=HEADERS,
                timeout=8,
            )
            data = (resp.json().get("dstx") or {}).get("data") or [[]]
            rows = data[0] if data else []
        except Exception as e:  # noqa: BLE001
            logger.warning("操盘必读获取失败 %s: %s", symbol, e)
            return []
        events: list[dict] = []
        for row in rows:
            subtype = row.get("SPECIFIC_EVENTTYPE") or ""
            if row.get("EVENT_TYPE") != "报表披露" and "预披露" not in subtype:
                continue
            ev_date = _parse_date(row.get("NOTICE_DATE"))
            if ev_date and ev_date >= today:
                events.append(_event(
                    ev_date, "disclosure", symbol, name,
                    subtype, row.get("LEVEL1_CONTENT") or ""
                ))
        return events

    events: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(fetch_one, meta):
            events += result
    return events


def disclosure_deadlines(market: str, today: date,
                         horizon_days: int = 400) -> list[dict]:
    """财报法定截止日（固定规则，全市场事件）。

    A股：三季报10/31，年报与一季报次年4/30，半年报8/31。
    港股：年报3/31，半年报8/31。
    """
    if market == "CN":
        rules = [
            ((10, 31), "三季报披露截止"),
            ((4, 30), "年报、一季报披露截止"),
            ((8, 31), "半年报披露截止"),
        ]
    elif market == "HK":
        rules = [
            ((3, 31), "年报披露截止"),
            ((8, 31), "半年报披露截止"),
        ]
    else:
        return []

    end = today + timedelta(days=horizon_days)
    events: list[dict] = []
    for year in range(today.year - 1, today.year + 3):
        for (month, day), label in rules:
            ev_date = date(year, month, day)
            if today <= ev_date <= end:
                events.append(_event(
                    ev_date, "deadline", "",
                    "全市场" if market == "CN" else "港股",
                    label, ""
                ))
    return events
