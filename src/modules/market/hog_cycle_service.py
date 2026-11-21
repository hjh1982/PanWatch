"""生猪周期：农业农村部月度/季度数据与中国养猪网日度价格。

农业农村部生猪专题的月度页面按「年/月」分页归档，页面内以表格给出当月的
能繁母猪存栏、生猪存栏（季度末值）、定点屠宰量、二元母猪/仔猪/生猪出场价等
指标；2021 年以来的页面全部可访问。中国养猪网提供最近约一年的外三元生猪、
玉米、豆粕日度均价，用于计算猪粮比。两处数据按日期合并：月度系列覆盖 2021
年至今，日度系列覆盖最近一年。
"""

from __future__ import annotations

import calendar
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Callable

import httpx

from src.modules.market.etf_share_service import _require_group, today_cn
from src.modules.market.statistics_store import StatisticsStore
from src.platform.marketdata.collectors.market_http import market_get

logger = logging.getLogger(__name__)

HOG_CYCLE_GROUP_ID = "hogCycle"

_BASE_URL = "https://www.moa.gov.cn/ztzl/szcpxx/jdsj"
_START_YEAR = 2021
_DAILY_URL = "https://xt.yangzhu.vip/data/getzhujiahitsdata"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_DAILY_HEADERS = {
    **_HEADERS,
    "Referer": "https://www.yangzhu.vip/",
    "X-Requested-With": "XMLHttpRequest",
}

# 2026 年修订后的正常保有量与绿色合理区间（万头）。
NORMAL_SOW = 3750
GREEN_LOW = 3450
GREEN_HIGH = 3863
# 猪粮比预警阈值（生猪价 ÷ 玉米价）。
RATIO_ALERT = (
    (5.0, "一级预警"),
    (6.0, "二级预警"),
    (9.0, "正常"),
    (10.0, "三级预警"),
    (12.0, "二级预警"),
)

_TTL_S = 6 * 3600
_store = StatisticsStore(namespace=HOG_CYCLE_GROUP_ID, default_ttl_sec=_TTL_S)
_lock = threading.Lock()

_YEAR_REDIRECT = re.compile(r"url=\./(\d{6})/")
_MONTH_URL = re.compile(r"objurl=\"(https?://[^\"]*/jdsj/(?:\d{4}/)?\d{6}/)\"")
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_MONTH_LABEL = re.compile(r"(\d{4})年(\d{1,2})月份")
_QUARTER_LABEL = re.compile(r"(\d{4})年(?:第)?(\d)季度末")

_FIELDS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("sow", lambda label: "能繁母猪存栏" in label),
    ("hog_stock", lambda label: "生猪存栏" in label),
    ("slaughter", lambda label: "生猪定点屠宰企业屠宰量" in label),
    ("bboar_price", lambda label: "二元母猪销售价格" in label),
    ("piglet_price", lambda label: "仔猪价格" in label),
    ("hog_price", lambda label: "生猪出场价格" in label),
    ("pork_wholesale", lambda label: "全国批发市场白条猪价格" in label),
)
_QUARTER_FIELDS = frozenset({"sow", "hog_stock"})


def month_end(key: str) -> str:
    year, month = (int(part) for part in key.split("-"))
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def _to_float(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _cell_text(cell: str) -> str:
    return re.sub(r"\s+", "", _TAG.sub("", cell)).strip()


def parse_month_page(page: Any) -> dict[str, dict[str, float]]:
    """解析农业农村部某个月的生猪数据表，返回 {YYYY-MM: {指标: 数值}}。"""
    if not isinstance(page, str) or not page:
        return {}
    rows: dict[str, dict[str, float]] = {}
    for row_html in _TR.findall(page):
        cells = _TD.findall(row_html)
        if len(cells) < 3:
            continue
        label = _cell_text(cells[1])
        if not label:
            continue
        field = next((name for name, match in _FIELDS if match(label)), None)
        if field is None:
            continue
        number = _NUMBER.search(_cell_text(cells[2]))
        value = _to_float(number.group(0)) if number else None
        if value is None:
            continue
        if field in _QUARTER_FIELDS:
            match = _QUARTER_LABEL.search(label)
            if not match:
                continue
            year = int(match.group(1))
            key = f"{year:04d}-{int(match.group(2)) * 3:02d}"
        else:
            match = _MONTH_LABEL.search(label)
            if not match:
                continue
            key = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
        rows.setdefault(key, {})[field] = value
    return rows


def parse_daily(payload: Any, field: str) -> dict[str, float]:
    """解析中国养猪网日度接口，返回 {YYYY-MM-DD: 价格}。"""
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("data")
    if not isinstance(rows, list):
        return {}
    result: dict[str, float] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        day = str(item.get("pricedate") or "").strip()
        value = _to_float(item.get(field))
        if day and value is not None:
            result[day] = value
    return result


def _fetch_text(url: str) -> str | None:
    return market_get(
        url,
        host_key="www.moa.gov.cn",
        headers=_HEADERS,
        parse="text",
        min_interval_s=0.15,
        timeout=20.0,
        total_timeout=40.0,
        symbol="MOA-hog",
        log_label="农业农村部生猪数据",
        force_ipv4=True,
    )


def _month_urls(year: int) -> dict[str, str]:
    year_page = _fetch_text(f"{_BASE_URL}/{year}/")
    if not year_page:
        return {}
    match = _YEAR_REDIRECT.search(year_page)
    seed = f"{_BASE_URL}/{year}/{match.group(1)}/" if match else None
    page = _fetch_text(seed) if seed else year_page
    found: dict[str, str] = {}
    for url in _MONTH_URL.findall(page or ""):
        month = url.rstrip("/").rsplit("/", 1)[-1]
        if len(month) != 6 or not month.startswith(str(year)):
            continue
        number = int(month[4:])
        if 1 <= number <= 12:
            found[f"{year:04d}-{number:02d}"] = url
    return found


def _target_month_urls(today: date) -> dict[str, str]:
    urls: dict[str, str] = {}
    for year in range(_START_YEAR, today.year + 1):
        urls.update(_month_urls(year))
    return dict(sorted(urls.items()))


def _fetch_months(urls: dict[str, str]) -> dict[str, dict[str, float]]:
    def worker(item: tuple[str, str]) -> tuple[str, dict[str, float]]:
        key, url = item
        page = _fetch_text(url)
        return key, (parse_month_page(page) or {}).get(key, {})

    result: dict[str, dict[str, float]] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for key, row in pool.map(worker, list(urls.items())):
            if row:
                result[key] = row
    return result


def _load_months(*, force: bool) -> dict[str, dict[str, float]]:
    if not force:
        cached = _store.get("months")
        if isinstance(cached, dict):
            return cached
    previous = _store.get("months", allow_stale=True)
    months = previous if isinstance(previous, dict) else {}
    recent = set(sorted(months)[-2:])
    urls = {
        key: url
        for key, url in _target_month_urls(today_cn()).items()
        if force or key not in months or key in recent
    }
    incoming = _fetch_months(urls)
    if incoming:
        merged = dict(months)
        for key, row in incoming.items():
            merged[key] = {**merged.get(key, {}), **row}
        months = merged
        _store.set("months", months)
    return months


def _fetch_daily(ptype: str, field: str) -> dict[str, float]:
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    with httpx.Client(
        transport=transport,
        headers=_DAILY_HEADERS,
        timeout=25.0,
        trust_env=True,
    ) as client:
        response = client.post(
            _DAILY_URL,
            data={"ptype": ptype, "datetype": "0"},
        )
        response.raise_for_status()
        payload = response.json()
    return parse_daily(payload, field)


def _refresh_daily() -> dict[str, dict[str, float]]:
    previous = _store.get("daily", allow_stale=True)
    daily = dict(previous) if isinstance(previous, dict) else {}
    specs = (
        ("hog_price", "1", "pigprice"),
        ("corn", "4", "maizeprice"),
        ("soybean_meal", "5", "bean"),
    )
    changed = False
    for key, ptype, field in specs:
        try:
            incoming = _fetch_daily(ptype, field)
        except Exception as exc:
            logger.warning("[生猪周期] %s 日度价格抓取失败: %s", key, exc)
            continue
        if incoming:
            daily[key] = {**daily.get(key, {}), **incoming}
            changed = True
    if changed:
        _store.set("daily", daily, ttl_sec=12 * 3600)
    return daily


def _aligned(
    months: dict[str, dict[str, float]],
    keys: list[str],
    field: str,
) -> list[float | None]:
    return [months.get(key, {}).get(field) for key in keys]


def _metric(
    metric_id: str,
    label: str,
    unit: str,
    pairs: list[tuple[str, float]],
) -> dict[str, Any] | None:
    if not pairs:
        return None
    latest_date, latest = pairs[-1]
    prior = pairs[-2][1] if len(pairs) > 1 else None
    delta = round(latest - prior, 4) if prior is not None else None
    delta_pct = (
        round(delta / prior * 100, 4) if delta is not None and prior else None
    )
    return {
        "id": metric_id,
        "label": label,
        "unit": unit,
        "axis": "left",
        "latest": round(latest, 4),
        "latest_date": latest_date,
        "prior": prior,
        "delta": delta,
        "delta_pct": delta_pct,
    }


def ratio_level(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    for threshold, label in RATIO_ALERT:
        if ratio < threshold:
            return label
    return "一级预警"


def _daily_ratio(
    daily: dict[str, dict[str, float]],
) -> tuple[list[str], list[float], list[float], list[float]]:
    pig = daily.get("hog_price", {})
    corn = daily.get("corn", {})
    dates = sorted(set(pig) & set(corn))
    ratios: list[float] = []
    hog_values: list[float] = []
    corn_values: list[float] = []
    for day in dates:
        corn_kg = round(corn[day] / 1000, 4)
        if corn_kg <= 0:
            continue
        hog_values.append(pig[day])
        corn_values.append(round(corn[day], 2))
        ratios.append(round(pig[day] / corn_kg, 3))
    return dates, ratios, hog_values, corn_values


def build_payload(
    months: dict[str, dict[str, float]],
    daily: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    group = _require_group(HOG_CYCLE_GROUP_ID)
    daily = daily or {}
    keys = sorted(key for key in months if months[key])
    dates = [month_end(key) for key in keys]

    def pairs(field: str) -> list[tuple[str, float]]:
        return [
            (month_end(key), months[key][field])
            for key in keys
            if months[key].get(field) is not None
        ]

    hog_pairs = pairs("hog_price")
    piglet_pairs = pairs("piglet_price")
    bboar_pairs = pairs("bboar_price")
    slaughter_pairs = pairs("slaughter")
    sow_pairs = pairs("sow")
    stock_pairs = pairs("hog_stock")
    ratio_dates, ratio_values, daily_hog, daily_corn = _daily_ratio(daily)
    ratio_pairs = list(zip(ratio_dates, ratio_values))

    metrics = [
        entry
        for entry in (
            _metric("sow", "能繁母猪存栏", "万头", sow_pairs),
            _metric("hog_price", "生猪出场价", "元/公斤", hog_pairs),
            _metric("piglet_price", "仔猪价格", "元/公斤", piglet_pairs),
            _metric("corn_hog_ratio", "猪粮比", ":1", ratio_pairs),
            _metric("slaughter", "定点屠宰量", "万头", slaughter_pairs),
        )
        if entry is not None
    ]

    series = {
        "hog_price": _aligned(months, keys, "hog_price"),
        "piglet_price": _aligned(months, keys, "piglet_price"),
        "bboar_price": _aligned(months, keys, "bboar_price"),
        "slaughter": _aligned(months, keys, "slaughter"),
    }
    quarterly = {
        "dates": [day for day, _ in sow_pairs],
        "sow": [value for _, value in sow_pairs],
        "hog_stock": [value for _, value in stock_pairs],
    }
    daily_block = {
        "dates": ratio_dates,
        "ratio": ratio_values,
        "hog_price": daily_hog,
        "corn": daily_corn,
    }
    sources: list[dict[str, Any]] = [
        {
            "id": "moa",
            "name": "农业农村部 · 生猪产品月度数据",
            "url": f"{_BASE_URL}/",
            "cadence": "月度 / 季度",
            "start": min(keys) if keys else None,
            "latest": max(keys) if keys else None,
        },
        {
            "id": "zhujia",
            "name": "中国养猪网 · 生猪与饲料日度价格",
            "url": "https://www.yangzhu.vip/",
            "cadence": "日度",
            "start": ratio_dates[0] if ratio_dates else None,
            "latest": ratio_dates[-1] if ratio_dates else None,
        },
    ]
    as_of = max(dates[-1] if dates else "", ratio_dates[-1] if ratio_dates else "") or None
    return {
        "group": group.id,
        "group_label": group.label,
        "scope": "能繁母猪存栏、生猪价格与猪粮比：农业农村部月度/季度数据（2021 年起）+ 中国养猪网日度价格",
        "unit": "万头 / 元/公斤 / :1",
        "as_of": as_of,
        "dates": dates,
        "metrics": metrics,
        "series": series,
        "quarterly": quarterly,
        "daily": daily_block,
        "policy": {
            "normal": NORMAL_SOW,
            "green_low": GREEN_LOW,
            "green_high": GREEN_HIGH,
        },
        "sources": sources,
        "coverage": {
            "trading_days": len(dates),
            "target": len(dates),
            "complete": True,
        },
    }


_refresh_flag_lock = threading.Lock()
_refreshing = False


def _cached(key: str, *, stale: bool = False) -> dict[str, Any] | None:
    value = _store.get(key, allow_stale=stale)
    return value if isinstance(value, dict) else None


def _refresh_snapshot() -> None:
    global _refreshing
    try:
        with _lock:
            _load_months(force=False)
            _refresh_daily()
    except Exception as exc:  # 后台刷新失败不应影响本次请求
        logger.warning("[生猪周期] 后台刷新失败: %s", exc)
    finally:
        with _refresh_flag_lock:
            _refreshing = False


def _schedule_refresh() -> None:
    global _refreshing
    with _refresh_flag_lock:
        if _refreshing:
            return
        _refreshing = True
    threading.Thread(
        target=_refresh_snapshot,
        name="hog-cycle-refresh",
        daemon=True,
    ).start()


def get_curve_payload(
    group_id: str,
    days: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    _require_group(group_id)
    if not force:
        months = _cached("months")
        daily = _cached("daily")
        if months is not None and daily is not None:
            return build_payload(months, daily)
        stale_months = _cached("months", stale=True)
        stale_daily = _cached("daily", stale=True)
        if stale_months is not None and stale_daily is not None:
            # 过期数据先返回，后台补齐；避免每次 TTL 到期都阻塞几十秒。
            _schedule_refresh()
            return build_payload(stale_months, stale_daily)
    with _lock:
        months = _load_months(force=force)
        daily = _refresh_daily()
    return build_payload(months, daily)
