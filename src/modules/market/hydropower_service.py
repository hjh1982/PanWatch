"""长江电力来水与发电量：六座梯级电站的季度披露。

长江电力（600900）按季度发布《发电量完成情况公告》，披露乌东德、三峡两库
的年内累计来水，以及乌东德、白鹤滩、溪洛渡、向家坝、三峡、葛洲坝六座电站
的当季与年内累计发电量。本服务从东方财富公告接口发现公告并读取公告 PDF 正文，
按年内累计值差分得到单季来水，口径自 2023 年一季度（六库联调完整运行）起。
缺失季度不补值。
"""

from __future__ import annotations

import io
import logging
import math
import re
import threading
from typing import Any

from pypdf import PdfReader

from src.modules.market.etf_share_service import _require_group
from src.modules.market.statistics_store import StatisticsStore, merge_mappings
from src.platform.marketdata.collectors.market_http import market_get

logger = logging.getLogger(__name__)

HYDROPOWER_GROUP_ID = "hydropower"

_LIST_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_PDF_URL = "https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf"
_NOTICE_PAGE = "https://data.eastmoney.com/notices/stock/600900.html"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_START_YEAR = 2023
_MAX_LIST_PAGES = 6
_CACHE_TTL_S = 6 * 3600

_NUMBER = r"([\d,]+(?:\.\d+)?)"
_PERIOD = re.compile(r"(\d{4})年")
_STATION_NAMES = ("乌东德", "白鹤滩", "溪洛渡", "向家坝", "三峡", "葛洲坝")
_STATION_KEYS = {
    "乌东德": "udongde",
    "白鹤滩": "baihetan",
    "溪洛渡": "xiluodu",
    "向家坝": "xiangjiaba",
    "三峡": "three_gorges",
    "葛洲坝": "gezhouba",
}

_store = StatisticsStore(namespace=HYDROPOWER_GROUP_ID, default_ttl_sec=_CACHE_TTL_S)
_lock = threading.Lock()


def _positive_float(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _compact(text: Any) -> str:
    return re.sub(r"\s+", "", text) if isinstance(text, str) else ""


def period_from_title(title: Any) -> str | None:
    """从公告标题提取 YYYYQn；只认发电量完成情况公告。"""
    if not isinstance(title, str) or "发电量" not in title or "1-2月" in title:
        return None
    match = _PERIOD.search(title)
    if match is None:
        return None
    year = int(match.group(1))
    if "第一季度" in title:
        quarter = 1
    elif "第二季度" in title or "半年度" in title:
        quarter = 2
    elif "第三季度" in title or "三季度" in title:
        quarter = 3
    elif "发电量完成情况公告" in title or "年发电量" in title:
        quarter = 4
    else:
        return None
    return f"{year}Q{quarter}"


def parse_announcement_list(payload: Any) -> list[dict[str, str]]:
    """从公告列表提取目标公告（去重、按期间升序）。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    found: dict[str, dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        period = period_from_title(item.get("title"))
        art_code = item.get("art_code")
        if period is None or not isinstance(art_code, str):
            continue
        if int(period[:4]) < _START_YEAR:
            continue
        found.setdefault(
            period,
            {
                "period": period,
                "art_code": art_code,
                "notice_date": str(item.get("notice_date") or "")[:10],
                "title": item.get("title") or "",
            },
        )
    return [found[period] for period in sorted(found)]


def parse_announcement(content: Any, period: str) -> dict[str, Any] | None:
    """解析公告正文，返回年内累计来水、累计发电量与当季分站发电量。"""
    text = _compact(content)
    if not text or len(period) < 6 or not period.endswith(tuple("1234")):
        return None
    quarter = int(period[-1])

    def value(pattern: str) -> float | None:
        match = re.search(pattern, text)
        return _positive_float(match.group(1)) if match else None

    cumulative_udongde = value(r"乌东德水库来水总量约" + _NUMBER + r"亿立方米")
    cumulative_three_gorges = value(r"三峡水库来水总量约" + _NUMBER + r"亿立方米")
    cumulative_total = value(r"总发电量约" + _NUMBER + r"亿千瓦时")

    total = None
    if quarter == 1:
        total = cumulative_total
    elif quarter in (2, 3, 4):
        label = {2: "二", 3: "三", 4: "四"}[quarter]
        total = value(
            r"第"
            + label
            + r"季度[^。]{0,160}?总发电量约"
            + _NUMBER
            + r"亿千瓦时"
        )

    stations: dict[str, float] = {}
    for name in _STATION_NAMES:
        station = value(name + r"电站[^0-9]{0,30}" + _NUMBER)
        if station is not None:
            stations[_STATION_KEYS[name]] = station

    if total is None and len(stations) == len(_STATION_NAMES):
        total = round(sum(stations.values()), 4)
    if total is None and cumulative_udongde is None:
        return None

    return {
        "period": period,
        "year": int(period[:4]),
        "quarter": quarter,
        "cumulative_udongde": cumulative_udongde,
        "cumulative_three_gorges": cumulative_three_gorges,
        "cumulative_total": cumulative_total,
        "total": total,
        "has_quarter_table": "第四季度" in text or "四季度" in text,
        "stations": stations,
    }


def build_rows(announcements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把年内累计披露换算为单季值；Q4 由年度公告减去前三季度得到。"""
    by_period = {
        item["period"]: item
        for item in announcements
        if isinstance(item, dict) and isinstance(item.get("period"), str)
    }
    rows: list[dict[str, Any]] = []
    for year in sorted({int(period[:4]) for period in by_period}):
        quarters = [by_period.get(f"{year}Q{number}") for number in range(1, 5)]
        previous_cumulative: dict[str, float | None] = {"udongde": None, "three_gorges": None}
        quarterly_totals: dict[int, float | None] = {}
        for index, item in enumerate(quarters):
            if item is None:
                continue
            quarter = index + 1
            if quarter == 4:
                total = item.get("total")
                stations = dict(item.get("stations") or {})
                if item.get("has_quarter_table"):
                    if total is None:
                        prior = [quarterly_totals.get(number) for number in (1, 2, 3)]
                        if all(isinstance(value, (int, float)) for value in prior):
                            total = _positive_float(
                                item.get("cumulative_total") - sum(prior)
                            )
                elif isinstance(item.get("cumulative_total"), (int, float)):
                    prior = [quarterly_totals.get(number) for number in (1, 2, 3)]
                    if all(isinstance(value, (int, float)) for value in prior):
                        total = _positive_float(
                            item["cumulative_total"] - sum(prior)
                        )
                        for key in _STATION_KEYS.values():
                            annual = stations.get(key)
                            parts = [
                                (quarters[number - 1] or {}).get("stations", {}).get(key)
                                for number in (1, 2, 3)
                            ]
                            if isinstance(annual, (int, float)) and all(
                                isinstance(part, (int, float)) for part in parts
                            ):
                                stations[key] = _positive_float(annual - sum(parts))
                inflow = {
                    "udongde": _difference(
                        item.get("cumulative_udongde"), previous_cumulative["udongde"]
                    ),
                    "three_gorges": _difference(
                        item.get("cumulative_three_gorges"),
                        previous_cumulative["three_gorges"],
                    ),
                }
            else:
                total = item.get("total")
                stations = dict(item.get("stations") or {})
                if quarter == 1:
                    inflow = {
                        "udongde": item.get("cumulative_udongde"),
                        "three_gorges": item.get("cumulative_three_gorges"),
                    }
                else:
                    inflow = {
                        "udongde": _difference(
                            item.get("cumulative_udongde"),
                            previous_cumulative["udongde"],
                        ),
                        "three_gorges": _difference(
                            item.get("cumulative_three_gorges"),
                            previous_cumulative["three_gorges"],
                        ),
                    }
            if quarter in (1, 2, 3):
                previous_cumulative["udongde"] = item.get("cumulative_udongde")
                previous_cumulative["three_gorges"] = item.get("cumulative_three_gorges")
            quarterly_totals[quarter] = total
            rows.append(
                {
                    "period": item["period"],
                    "generation": total,
                    "inflow_udongde": inflow["udongde"],
                    "inflow_three_gorges": inflow["three_gorges"],
                    "stations": stations,
                }
            )
    return rows


def _difference(current: Any, previous: Any) -> float | None:
    if not isinstance(current, (int, float)) or not isinstance(previous, (int, float)):
        return None
    return _positive_float(current - previous)


def _fetch_pdf_text(art_code: str) -> str | None:
    content = market_get(
        _PDF_URL.format(art_code=art_code),
        host_key="pdf.dfcfw.com",
        headers=_HEADERS,
        parse="content",
        timeout=30.0,
        total_timeout=60.0,
        symbol="600900",
        log_label="长电来水发电公告PDF",
    )
    if not isinstance(content, bytes) or not content:
        return None
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        logger.warning(f"长电来水发电公告PDF解析失败 {art_code}: {exc}")
        return None


def _fetch_announcements() -> list[dict[str, Any]]:
    found: dict[str, dict[str, str]] = {}
    for page in range(1, _MAX_LIST_PAGES + 1):
        payload = market_get(
            _LIST_URL,
            host_key="np-anotice-stock.eastmoney.com",
            params={
                "sr": -1,
                "page_size": 100,
                "page_index": page,
                "ann_type": "A",
                "client_source": "web",
                "stock_list": "600900",
                "f_node": 0,
                "s_node": 0,
            },
            headers=_HEADERS,
            parse="json",
            timeout=20.0,
            total_timeout=40.0,
            symbol="600900",
            log_label="长电来水发电公告列表",
        )
        items = parse_announcement_list(payload)
        for item in items:
            found.setdefault(item["period"], item)
        if _list_is_old(payload):
            break
    parsed: list[dict[str, Any]] = []
    for period in sorted(found):
        item = found[period]
        try:
            record = parse_announcement(_fetch_pdf_text(item["art_code"]), period)
        except Exception as exc:
            logger.warning("[长电来水发电] %s 公告抓取失败: %s", period, exc)
            continue
        if record is not None:
            parsed.append(record)
    return parsed


def _list_is_old(payload: Any) -> bool:
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return True
    dates = [
        str(item.get("notice_date") or "")[:4]
        for item in items
        if isinstance(item, dict)
    ]
    years = [int(year) for year in dates if year.isdigit()]
    return bool(years) and min(years) < _START_YEAR


def load_rows(*, force: bool = False) -> list[dict[str, Any]]:
    if not force:
        cached = _store.get("announcements")
        if isinstance(cached, list):
            return build_rows(cached)
    with _lock:
        if not force:
            cached = _store.get("announcements")
            if isinstance(cached, list):
                return build_rows(cached)
        previous = _store.get("announcements", allow_stale=True)
        announcements = previous if isinstance(previous, list) else []
        try:
            incoming = _fetch_announcements()
        except Exception as exc:
            logger.warning("[长电来水发电] 公告抓取失败: %s", exc)
        else:
            if incoming:
                # 合并原始累计披露后统一差分，不能用新季度结果覆盖旧季度结果。
                by_period = merge_mappings(
                    {item["period"]: item for item in announcements},
                    {item["period"]: item for item in incoming},
                )
                announcements = [by_period[period] for period in sorted(by_period)]
                _store.set("announcements", announcements)
        return build_rows(announcements)


def _series(rows: list[dict[str, Any]], key: str) -> list[float | None]:
    values: list[float | None] = []
    for row in rows:
        if key in ("generation", "inflow_udongde", "inflow_three_gorges"):
            value = row.get(key)
        else:
            value = (row.get("stations") or {}).get(key)
        values.append(round(value, 4) if isinstance(value, (int, float)) else None)
    return values


def _metric(
    metric_id: str,
    label: str,
    unit: str,
    dates: list[str],
    values: list[float | None],
) -> dict[str, Any] | None:
    points = [(date, value) for date, value in zip(dates, values) if value is not None]
    if not points:
        return None
    latest_date, latest = points[-1]
    prior = points[-2][1] if len(points) >= 2 else None
    delta = round(latest - prior, 4) if prior is not None else None
    delta_pct = round(delta / prior * 100, 4) if delta is not None and prior else None
    return {
        "id": metric_id,
        "label": label,
        "unit": unit,
        "axis": "left",
        "latest": round(latest, 4),
        "latest_date": latest_date,
        "prior": round(prior, 4) if prior is not None else None,
        "delta": delta,
        "delta_pct": delta_pct,
    }


def build_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    group = _require_group(HYDROPOWER_GROUP_ID)
    dates = [row["period"] for row in rows]
    generation = _series(rows, "generation")
    inflow_udongde = _series(rows, "inflow_udongde")
    inflow_three_gorges = _series(rows, "inflow_three_gorges")
    station_series = {
        key: _series(rows, key) for key in _STATION_KEYS.values()
    }
    metrics = [
        item
        for item in (
            _metric("generation", "总发电量", "亿千瓦时", dates, generation),
            _metric("inflow_udongde", "乌东德来水", "亿立方米", dates, inflow_udongde),
            _metric(
                "inflow_three_gorges", "三峡来水", "亿立方米", dates, inflow_three_gorges
            ),
        )
        if item is not None
    ]
    sources = [
        {
            "id": "eastmoney",
            "name": "东方财富 · 长江电力发电量公告",
            "url": _NOTICE_PAGE,
            "cadence": "季度",
            "start": dates[0] if dates else None,
            "latest": dates[-1] if dates else None,
        }
    ]
    latest = rows[-1] if rows else None
    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "长江电力六座梯级电站季度来水与发电量",
        "unit": "亿千瓦时",
        "as_of": latest["period"] if latest else None,
        "dates": dates,
        "metrics": metrics,
        "series": {
            "generation": generation,
            "inflow_udongde": inflow_udongde,
            "inflow_three_gorges": inflow_three_gorges,
            **station_series,
        },
        "sources": sources,
        "coverage": {
            "trading_days": len(dates),
            "target": len(dates),
            "complete": True,
        },
    }


def get_curve_payload(
    group_id: str,
    days: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    _require_group(group_id)
    return build_payload(load_rows(force=force))
