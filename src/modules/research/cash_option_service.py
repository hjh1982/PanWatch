"""A 股现金选择权机会。

数据来自巨潮资讯（cninfo）公告全文检索：先按「现金选择权」检索近一年多的公告，
再对每家公司挑一份实施公告（无实施公告时退回最早一次提示性公告），从 PDF 中解析
行权价格、申报期与股权登记日；最后用现有行情链路补充现价，计算行权价相对现价
的溢价与年化。

口径说明：
- 只覆盖 A 股（沪深主板/创业板/科创板），B 股不参与。
- 现金选择权分「异议股东」与「终止上市全体股东」两类，资格条件不同，页面只做提示。
- 申报期内股票常停牌，现价可能取不到，此时退回公告披露的收盘价作参考价。
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from pypdf import PdfReader

from src.platform.marketdata.collectors.market_http import market_get

logger = logging.getLogger(__name__)

CASH_OPTION_CACHE_TTL_S = 3 * 3600
_LOOKBACK_DAYS = 420
_CHUNK_DAYS = 120
_RECENT_ENDED_DAYS = 120
_MAX_PAGES = 5

_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
_PDF_BASE = "http://static.cninfo.com.cn/"
_SEARCH_PAGE = "https://www.cninfo.com.cn/new/fulltextSearch?notautosubmit=&keyWord=%E7%8E%B0%E9%87%91%E9%80%89%E6%8B%A9%E6%9D%83"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
}

_A_SHARE_RE = re.compile(r"^(00[0-3]|30[0-1]|60[0-5]|688)\d{3}$")
_WINDOW_RE = re.compile(
    r"申报(?:期间|时间|日)[为：:]?\s*(20\d{2})年(\d{1,2})月(\d{1,2})日"
    r"\s*(?:至|到|—|-)\s*(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日"
)
_DATE_RE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
_PRICE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)元/股")
_REF_PRICE_PATTERNS = (
    re.compile(r"收盘价为([0-9]+(?:\.[0-9]+)?)元/股"),
    re.compile(r"收盘价分别为([0-9]+(?:\.[0-9]+)?)元/股"),
)
_APPLY_CODE_RE = re.compile(r"申报(?:编号|代码)[为：:]?\s*([0-9]{6})")
_APPLY_A_CODE_RE = re.compile(r"A股[^。；]{0,40}?现金选择权代码[为：:]?\s*([0-9]{6})")

_cache_lock = threading.Lock()
_cache_payload: dict[str, Any] | None = None
_cache_at: float = 0.0


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "")


def _iso(year: str | int, month: str | int, day: str | int) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _strip_tags(value: str | None) -> str:
    return re.sub(r"<[^>]+>", "", value or "")


def is_a_share(code: str) -> bool:
    return bool(_A_SHARE_RE.match(code or ""))


def parse_cash_option_text(text: str | None) -> dict[str, Any]:
    """从现金选择权公告正文解析价格、申报期、登记日等关键字段。"""
    flat = _norm(text)
    if not flat:
        return {
            "exercise_price": None,
            "ref_price": None,
            "window_start": None,
            "window_end": None,
            "record_date": None,
            "apply_code": None,
            "eligibility": "unknown",
        }

    exercise_price = None
    for marker in ("行权价格", "现金选择权价格", "现金选择权行权价"):
        for match in re.finditer(marker, flat):
            segment = flat[match.start() : match.start() + 80]
            price = _PRICE_RE.search(segment)
            if price and "港币" not in segment[: price.end()]:
                exercise_price = float(price.group(1))
                break
        if exercise_price is not None:
            break
    if exercise_price is None:
        match = re.search(r"A股[^。；]{0,6}?([0-9]+(?:\.[0-9]+)?)元/股", flat)
        if match:
            exercise_price = float(match.group(1))

    ref_price = None
    for pattern in _REF_PRICE_PATTERNS:
        match = pattern.search(flat)
        if match:
            ref_price = float(match.group(1))
            break

    window_start = window_end = None
    match = _WINDOW_RE.search(flat)
    if match:
        window_start = _iso(match.group(1), match.group(2), match.group(3))
        end_year = match.group(4) or match.group(1)
        window_end = _iso(end_year, match.group(5), match.group(6))

    record_date = None
    for match in re.finditer("股权登记日", flat):
        if "现金选择权" not in flat[max(0, match.start() - 48) : match.start()]:
            continue
        segment = flat[match.start() : match.start() + 32]
        found = _DATE_RE.search(segment)
        if found:
            record_date = _iso(found.group(1), found.group(2), found.group(3))
            break

    apply_match = _APPLY_CODE_RE.search(flat)
    if apply_match is None:
        apply_match = _APPLY_A_CODE_RE.search(flat)
    if "异议股东" in flat:
        eligibility = "dissenting"
    elif "全体A股" in flat or "全体股东" in flat:
        eligibility = "all"
    else:
        eligibility = "unknown"

    return {
        "exercise_price": exercise_price,
        "ref_price": ref_price,
        "window_start": window_start,
        "window_end": window_end,
        "record_date": record_date,
        "apply_code": apply_match.group(1) if apply_match else None,
        "eligibility": eligibility,
    }


def classify_status(
    window_start: str | None,
    window_end: str | None,
    today: date | None = None,
) -> str:
    current = today or date.today()
    if window_start:
        start = date.fromisoformat(window_start)
        if current < start:
            return "upcoming"
    if window_end:
        end = date.fromisoformat(window_end)
        if current <= end:
            return "active"
        return "ended"
    if window_start:
        return "active" if current >= date.fromisoformat(window_start) else "upcoming"
    return "unknown"


def _method(title: str, text: str) -> str:
    source = f"{title}{text[:400]}"
    if "终止上市" in source or "终止公司股票" in source or "主动终止" in source:
        return "delisting"
    if "吸收合并" in source or "合并" in source:
        return "merger"
    return "other"


def _month_chunks(today: date, lookback_days: int = _LOOKBACK_DAYS):
    end = today
    earliest = today - timedelta(days=lookback_days)
    while end >= earliest:
        start = max(earliest, end - timedelta(days=_CHUNK_DAYS))
        yield start, end
        end = start - timedelta(days=1)


def fetch_cninfo_announcements(today: date | None = None) -> list[dict[str, Any]]:
    """按时间分片检索巨潮现金选择权公告，按公告 URL 去重。"""
    current = today or date.today()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    ok_chunks = 0
    for start, end in _month_chunks(current):
        payload = {
            "pageNum": 1,
            "pageSize": 100,
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": "现金选择权",
            "seDate": f"{start.isoformat()}~{end.isoformat()}",
            "category": "",
            "trade": "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        for page in range(1, _MAX_PAGES + 1):
            payload["pageNum"] = page
            try:
                with httpx.Client(timeout=20.0, headers=_HEADERS, trust_env=True) as client:
                    response = client.post(_QUERY_URL, data=payload)
                    response.raise_for_status()
                    body = response.json()
            except Exception as exc:
                logger.warning(f"巨潮现金选择权公告检索失败 {start}~{end} p{page}: {exc}")
                break
            ok_chunks += 1
            items = body.get("announcements") or []
            for item in items:
                url = item.get("adjunctUrl")
                title = _strip_tags(item.get("announcementTitle"))
                if not url or url in seen or "现金选择权" not in title:
                    continue
                seen.add(url)
                code = str(item.get("secCode") or "")
                if not is_a_share(code):
                    continue
                timestamp = item.get("announcementTime")
                if isinstance(timestamp, (int, float)):
                    announced = datetime.fromtimestamp(timestamp / 1000, timezone.utc).date()
                else:
                    announced = current
                rows.append(
                    {
                        "code": code,
                        "name": item.get("secName") or code,
                        "title": title,
                        "date": announced.isoformat(),
                        "url": _PDF_BASE + str(url),
                    }
                )
            total = int(body.get("totalRecordNum") or 0)
            if len(items) < 100 or page * 100 >= total:
                break
    if ok_chunks == 0:
        raise RuntimeError("巨潮现金选择权公告接口不可用")
    return rows


def _announcement_priority(title: str) -> int | None:
    if "英文" in title or "现金选择权" not in title:
        return None
    if any(word in title for word in ("结果", "清算", "交割", "派发", "摘牌")):
        return None
    if "提示" in title:
        return 1
    if "实施" in title:
        return 0
    return 2


def _fetch_pdf_text(url: str) -> str | None:
    content = market_get(
        url,
        host_key="static.cninfo.com.cn",
        headers={"User-Agent": _HEADERS["User-Agent"]},
        parse="content",
        timeout=30.0,
        total_timeout=60.0,
        log_label="现金选择权公告PDF",
    )
    if not isinstance(content, bytes) or not content:
        return None
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)
    except Exception as exc:
        logger.warning(f"现金选择权公告PDF解析失败 {url}: {exc}")
        return None


def _quote_map(codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    try:
        from src.platform.marketdata.marketdata_client import md_quote_rows

        rows = md_quote_rows(codes, "CN")
    except Exception as exc:
        logger.warning(f"现金选择权现价获取失败: {exc}")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if symbol:
            result[symbol] = row
    return result


def _build_events(
    announcements: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    today: date,
    recent_days: int = _RECENT_ENDED_DAYS,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in announcements:
        grouped.setdefault(row["code"], []).append(row)

    events: list[dict[str, Any]] = []
    for code, rows in grouped.items():
        candidates = []
        for row in rows:
            priority = _announcement_priority(row["title"])
            if priority is not None:
                candidates.append((priority, row["date"], row))
        candidates.sort(key=lambda item: (item[0], item[1]))
        if not candidates:
            continue

        parsed: dict[str, Any] | None = None
        source: dict[str, Any] | None = None
        method = "other"
        for _, _, row in candidates[:3]:
            text = _fetch_pdf_text(row["url"])
            if not text:
                continue
            candidate = parse_cash_option_text(text)
            if candidate["exercise_price"] is None and source is not None:
                continue
            parsed = candidate
            source = row
            method = _method(row["title"], text)
            if candidate["exercise_price"] is not None and candidate["window_end"]:
                break
        if parsed is None or source is None:
            continue

        status = classify_status(parsed["window_start"], parsed["window_end"], today)
        if status == "ended":
            end = date.fromisoformat(parsed["window_end"]) if parsed["window_end"] else None
            if not end or (today - end).days > recent_days:
                continue
        elif status == "unknown":
            if (today - date.fromisoformat(source["date"])).days > recent_days:
                continue

        quote = quotes.get(code) or {}
        quote_price = quote.get("current_price")
        if not isinstance(quote_price, (int, float)) or quote_price <= 0:
            quote_price = None
        basis_price = quote_price if quote_price is not None else parsed["ref_price"]
        basis = "quote" if quote_price is not None else ("reference" if basis_price else None)

        exercise_price = parsed["exercise_price"]
        premium_pct = None
        if exercise_price and basis_price:
            premium_pct = round((exercise_price / basis_price - 1) * 100, 2)

        days_left = None
        annualized_pct = None
        if parsed["window_end"]:
            end = date.fromisoformat(parsed["window_end"])
            if end >= today:
                days_left = (end - today).days
                if premium_pct is not None and days_left > 0:
                    annualized_pct = round(premium_pct * 365 / days_left, 2)

        eligibility = parsed["eligibility"]
        if eligibility == "unknown" and "异议股东" in source["title"]:
            eligibility = "dissenting"
        if eligibility == "unknown" and method == "delisting":
            eligibility = "all"

        events.append(
            {
                "code": code,
                "name": quote.get("name") or source["name"],
                "market": "SH" if code.startswith("6") else "SZ",
                "method": method,
                "exercise_price": exercise_price,
                "quote_price": quote_price,
                "quote_date": quote.get("quote_date"),
                "reference_price": parsed["ref_price"],
                "basis": basis,
                "premium_pct": premium_pct,
                "annualized_pct": annualized_pct,
                "record_date": parsed["record_date"],
                "window_start": parsed["window_start"],
                "window_end": parsed["window_end"],
                "status": status,
                "days_left": days_left,
                "eligibility": eligibility,
                "apply_code": parsed["apply_code"],
                "announcement_title": source["title"],
                "announcement_date": source["date"],
                "announcement_url": source["url"],
            }
        )
    return events


def _sort_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = sorted(
        (item for item in events if item["status"] == "active"),
        key=lambda item: item.get("days_left") if item.get("days_left") is not None else 10**6,
    )
    upcoming = sorted(
        (item for item in events if item["status"] == "upcoming"),
        key=lambda item: item.get("window_start") or "9999-12-31",
    )
    unknown = sorted(
        (item for item in events if item["status"] == "unknown"),
        key=lambda item: item.get("announcement_date") or "",
        reverse=True,
    )
    ended = sorted(
        (item for item in events if item["status"] == "ended"),
        key=lambda item: item.get("window_end") or "",
        reverse=True,
    )
    return active + upcoming + unknown + ended


def build_payload(
    events: list[dict[str, Any]],
    today: date | None = None,
    *,
    error: str = "",
) -> dict[str, Any]:
    current = today or date.today()
    ordered = _sort_events(events)
    active = [item for item in ordered if item["status"] == "active"]
    upcoming = [item for item in ordered if item["status"] == "upcoming"]
    ended = [item for item in ordered if item["status"] == "ended"]
    premiums = [
        item["premium_pct"]
        for item in active + upcoming
        if isinstance(item["premium_pct"], (int, float))
    ]
    latest = max((item["announcement_date"] for item in ordered), default=None)
    return {
        "as_of": current.isoformat(),
        "summary": {
            "active": len(active),
            "upcoming": len(upcoming),
            "recent_ended": len(ended),
            "best_premium_pct": round(max(premiums), 2) if premiums else None,
        },
        "events": ordered,
        "error": error,
        "sources": [
            {
                "id": "cninfo",
                "name": "巨潮资讯 · 现金选择权公告",
                "url": _SEARCH_PAGE,
                "cadence": "事件驱动",
                "latest": latest,
            }
        ],
    }


def _load_events(today: date) -> tuple[list[dict[str, Any]], str]:
    try:
        announcements = fetch_cninfo_announcements(today)
    except Exception as exc:
        logger.warning(f"现金选择权公告抓取失败: {exc}")
        return [], "fetch_failed"
    quotes = _quote_map(sorted({row["code"] for row in announcements}))
    return _build_events(announcements, quotes, today), ""


def get_payload(force: bool = False) -> dict[str, Any]:
    global _cache_payload, _cache_at
    with _cache_lock:
        fresh = _cache_payload is not None and time.time() - _cache_at < CASH_OPTION_CACHE_TTL_S
        if fresh and not force:
            return _cache_payload  # type: ignore[return-value]
        today = date.today()
        events, error = _load_events(today)
        payload = build_payload(events, today, error=error)
        _cache_payload = payload
        _cache_at = time.time()
        return payload
