"""煤炭月度：规上工业原煤产量与煤炭、电力行业累计利润（国家统计局）。"""

from __future__ import annotations

import csv
import html as html_lib
import logging
import math
import re
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.modules.market.etf_share_service import _require_group, today_cn
from src.platform.persistence.database import DB_PATH
from src.platform.persistence.models import CoalMonthlyPoint

logger = logging.getLogger(__name__)

COAL_MONTHLY_GROUP_ID = "coalMonthly"
COAL_POWER_GROUP_ID = "coalPowerProfit"
_CSV = Path(__file__).with_name("resources") / "coal_monthly.csv"
_OUTPUT_METRIC = "coal_output"
_PROFIT_METRIC = "coal_profit"
_PER_TON_METRIC = "coal_profit_per_ton"
_POWER_PROFIT_METRIC = "power_profit"
_TOTAL_PROFIT_METRIC = "industry_profit_total"

_NBS_HOST = "https://www.stats.gov.cn"
_NBS_LIST = _NBS_HOST + "/sj/zxfb/"
_NBS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
_TITLE_OUTPUT = re.compile(
    r"^(20\d{2})年(?:(\d{1,2})月份|1—(\d{1,2})月份)规模以上工业增加值"
)
_TITLE_PROFIT = re.compile(
    r"^(20\d{2})年(?:1—(\d{1,2})月份)?(?:全国)?规模以上工业企业利润"
)
_SYNC_PAGES = 4
_SYNC_MONTHS = 3


class InvalidCoalMonth(ValueError):
    pass


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "—", "…", "..."):
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_month(value: str) -> bool:
    return bool(re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", value))


def load_seed_rows(path: Path = _CSV) -> list[dict[str, Any]]:
    """读取内置统计局月度种子数据；产量单位万吨，利润单位亿元。"""
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            raw = list(csv.DictReader(handle))
    except OSError:
        logger.warning("[煤炭月度] 种子文件不可读：%s", path)
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        month = (item.get("month") or "").strip()
        output = _to_float(item.get("output_10k_t"))
        if not _is_month(month) or output is None or output <= 0:
            continue
        rows.append(
            {
                "month": month,
                "output_10k_t": output,
                "output_cum_10k_t": _to_float(item.get("output_cum_10k_t")) or output,
                "profit_cum_yi": _to_float(item.get("profit_cum_yi")),
                "power_profit_cum_yi": _to_float(
                    item.get("power_profit_cum_yi")
                ),
            }
        )
    if not rows:
        logger.warning("[煤炭月度] 种子文件为空：%s", path)
    return rows


def _ensure_seeded(db: Session) -> None:
    rows = load_seed_rows()
    if not rows:
        return
    existing = {
        record.month: record for record in db.query(CoalMonthlyPoint).all()
    }
    if not existing:
        db.add_all(
            CoalMonthlyPoint(**row)
            for row in rows
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        return
    # 旧库补列后，用种子回填缺失的电力利润，不覆盖已同步的数据。
    changed = False
    for row in rows:
        record = existing.get(row["month"])
        power_cum = row.get("power_profit_cum_yi")
        if (
            record is not None
            and record.power_profit_cum_yi is None
            and power_cum is not None
        ):
            record.power_profit_cum_yi = power_cum
            changed = True
    if not changed:
        return
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def load_points(db: Session) -> list[dict[str, Any]]:
    _ensure_seeded(db)
    records = (
        db.query(
            CoalMonthlyPoint.month,
            CoalMonthlyPoint.output_10k_t,
            CoalMonthlyPoint.output_cum_10k_t,
            CoalMonthlyPoint.profit_cum_yi,
            CoalMonthlyPoint.power_profit_cum_yi,
        )
        .order_by(CoalMonthlyPoint.month)
        .all()
    )
    return [
        {
            "month": str(month),
            "output_10k_t": float(output),
            "output_cum_10k_t": (
                float(output_cum) if output_cum is not None else float(output)
            ),
            "profit_cum_yi": (
                float(profit_cum) if profit_cum is not None else None
            ),
            "power_profit_cum_yi": (
                float(power_profit_cum)
                if power_profit_cum is not None
                else None
            ),
        }
        for month, output, output_cum, profit_cum, power_profit_cum in records
    ]


def _table_rows(text: str):
    text = re.sub(r"<script.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
    for table in re.findall(r"<table.*?</table>", text, flags=re.S | re.I):
        for tr in re.findall(r"<tr.*?</tr>", table, flags=re.S | re.I):
            cells = [
                re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", cell)))
                .replace("\u3000", " ")
                .strip()
                for cell in re.findall(r"<t[dh].*?</t[dh]>", tr, flags=re.S | re.I)
            ]
            if cells:
                yield cells


def parse_coal_output_table(text: str) -> tuple[float, float] | None:
    for cells in _table_rows(text):
        if not cells[0].startswith("原煤"):
            continue
        numbers = [value for value in (_to_float(cell) for cell in cells[1:]) if value is not None]
        if not numbers:
            continue
        output = numbers[0]
        cumulative = numbers[2] if len(numbers) >= 4 else output
        if output > 0 and cumulative > 0:
            return output, cumulative
    return None


def parse_industry_profit_table(text: str) -> dict[str, float]:
    """提取煤炭开采洗选业与电力热力生产供应业的累计利润（亿元）。"""
    result: dict[str, float] = {}
    for cells in _table_rows(text):
        head = cells[0]
        if head == "煤炭开采和洗选业":
            key = "profit_cum_yi"
        elif head == "电力、热力生产和供应业":
            key = "power_profit_cum_yi"
        else:
            continue
        numbers = [
            value for value in (_to_float(cell) for cell in cells[1:]) if value is not None
        ]
        if len(numbers) >= 5:
            result[key] = numbers[4]
    return result


def parse_coal_profit_table(text: str) -> float | None:
    return parse_industry_profit_table(text).get("profit_cum_yi")


def _classify_title(title: str) -> tuple[str, int, int] | None:
    clean = re.sub(r"\s+", "", title)
    match = _TITLE_OUTPUT.match(clean)
    if match:
        month = int(match.group(2) or match.group(3) or 0)
        if 1 <= month <= 12:
            return "output", int(match.group(1)), month
    match = _TITLE_PROFIT.match(clean)
    if match:
        month = int(match.group(2) or 12)
        if 1 <= month <= 12:
            return "profit", int(match.group(1)), month
    return None


def parse_article(title: str, text: str) -> dict[str, Any] | None:
    meta = _classify_title(title)
    if meta is None:
        return None
    kind, year, month_num = meta
    month = f"{year}-{month_num:02d}"
    if kind == "output":
        parsed = parse_coal_output_table(text)
        if parsed is None:
            return None
        output, cumulative = parsed
        return {
            "month": month,
            "output_10k_t": output,
            "output_cum_10k_t": cumulative,
        }
    profits = parse_industry_profit_table(text)
    if not profits:
        return None
    return {"month": month, **profits}


def _fetch(url: str) -> str:
    with httpx.Client(
        timeout=25,
        follow_redirects=True,
        headers=_NBS_HEADERS,
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def _discover_articles() -> list[tuple[str, str]]:
    found: dict[str, tuple[int, str]] = {}
    for page in range(_SYNC_PAGES):
        url = _NBS_LIST if page == 0 else f"{_NBS_LIST}index_{page}.html"
        try:
            text = _fetch(url)
        except Exception as exc:
            logger.warning("[煤炭月度] 统计局列表页不可读 %s：%s", url, exc)
            break
        for href, raw_title in re.findall(
            r'<a[^>]+href="(\./20[0-9]{4}/t[0-9_]+\.html)"[^>]*>(.*?)</a>',
            text,
            flags=re.S,
        ):
            title = re.sub(
                r"\s+", "", html_lib.unescape(re.sub(r"<[^>]+>", "", raw_title))
            )
            meta = _classify_title(title)
            if meta is None:
                continue
            _, year, month_num = meta
            found[title] = (year * 100 + month_num, urljoin(url, href))
    ranked = sorted(found.items(), key=lambda item: item[1][0], reverse=True)
    return [(title, link) for title, (_, link) in ranked[:_SYNC_MONTHS * 2]]


def fetch_latest_articles() -> list[dict[str, Any]]:
    parsed: dict[str, dict[str, Any]] = {}
    for title, url in _discover_articles():
        try:
            article = parse_article(title, _fetch(url))
        except Exception as exc:
            logger.warning("[煤炭月度] 统计局文章不可读 %s：%s", url, exc)
            continue
        if article is None:
            continue
        entry = parsed.setdefault(article["month"], {"month": article["month"]})
        entry.update({key: value for key, value in article.items() if key != "month"})
    return [parsed[month] for month in sorted(parsed)]


def sync_from_nbs(db: Session) -> int:
    try:
        articles = fetch_latest_articles()
    except Exception as exc:
        logger.warning("[煤炭月度] 同步统计局数据失败，沿用本地数据：%s", exc)
        return 0
    if not articles:
        return 0
    changed = 0
    for article in articles:
        month = article["month"]
        record = (
            db.query(CoalMonthlyPoint)
            .filter(CoalMonthlyPoint.month == month)
            .first()
        )
        if record is None:
            if article.get("output_10k_t") is None:
                continue
            db.add(
                CoalMonthlyPoint(
                    month=month,
                    output_10k_t=article["output_10k_t"],
                    output_cum_10k_t=article.get(
                        "output_cum_10k_t", article["output_10k_t"]
                    ),
                    profit_cum_yi=article.get("profit_cum_yi"),
                    power_profit_cum_yi=article.get("power_profit_cum_yi"),
                )
            )
            changed += 1
            continue
        for field in (
            "output_10k_t",
            "output_cum_10k_t",
            "profit_cum_yi",
            "power_profit_cum_yi",
        ):
            value = article.get(field)
            if value is not None and getattr(record, field) != value:
                setattr(record, field, value)
                changed += 1
    if not changed:
        return 0
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.warning("[煤炭月度] 同步写入冲突，沿用本地数据")
        return 0
    return changed


def _build_series(points: list[dict[str, Any]]) -> dict[str, list]:
    months: list[str] = []
    output: list[float] = []
    output_cum: list[float] = []
    profit_cum: list[float | None] = []
    profit: list[float | None] = []
    per_ton: list[float | None] = []
    per_ton_cum: list[float | None] = []
    power_cum: list[float | None] = []
    power: list[float | None] = []
    total: list[float | None] = []
    total_cum: list[float | None] = []

    previous_year: str | None = None
    previous_cum: float | None = None
    previous_power_cum: float | None = None
    for point in points:
        year = point["month"][:4]
        cumulative = point["profit_cum_yi"]
        power_cumulative = point["power_profit_cum_yi"]
        if cumulative is None:
            monthly_profit = None
        elif previous_year == year and previous_cum is not None:
            monthly_profit = round(cumulative - previous_cum, 2)
        else:
            monthly_profit = round(cumulative, 2)
        if power_cumulative is None:
            power_monthly = None
        elif previous_year == year and previous_power_cum is not None:
            power_monthly = round(power_cumulative - previous_power_cum, 2)
        else:
            power_monthly = round(power_cumulative, 2)

        months.append(point["month"])
        output.append(point["output_10k_t"])
        output_cum.append(point["output_cum_10k_t"])
        profit_cum.append(cumulative)
        profit.append(monthly_profit)
        power_cum.append(power_cumulative)
        power.append(power_monthly)
        per_ton.append(
            round(monthly_profit * 1e4 / point["output_10k_t"], 2)
            if monthly_profit is not None
            else None
        )
        per_ton_cum.append(
            round(cumulative * 1e4 / point["output_cum_10k_t"], 2)
            if cumulative is not None and point["output_cum_10k_t"]
            else None
        )
        total.append(
            round(monthly_profit + power_monthly, 2)
            if monthly_profit is not None and power_monthly is not None
            else None
        )
        total_cum.append(
            round(cumulative + power_cumulative, 2)
            if cumulative is not None and power_cumulative is not None
            else None
        )
        previous_year = year
        if cumulative is not None:
            previous_cum = cumulative
        if power_cumulative is not None:
            previous_power_cum = power_cumulative

    return {
        "months": months,
        _OUTPUT_METRIC: output,
        "coal_output_cum": output_cum,
        _PROFIT_METRIC: profit,
        "coal_profit_cum": profit_cum,
        _PER_TON_METRIC: per_ton,
        "coal_profit_per_ton_cum": per_ton_cum,
        _POWER_PROFIT_METRIC: power,
        "power_profit_cum": power_cum,
        _TOTAL_PROFIT_METRIC: total,
        "industry_profit_total_cum": total_cum,
    }


def _metric(
    metric_id: str,
    months: list[str],
    values: list[float | None],
    *,
    digits: int,
) -> dict[str, Any] | None:
    indexes = [
        index
        for index, value in enumerate(values)
        if isinstance(value, (int, float)) and math.isfinite(value)
    ]
    if not indexes:
        return None
    latest_index = indexes[-1]
    latest = float(values[latest_index])
    previous = float(values[indexes[-2]]) if len(indexes) > 1 else None
    return {
        "id": metric_id,
        "latest": round(latest, digits),
        "latest_date": months[latest_index],
        "delta": round(latest - previous, digits) if previous is not None else None,
        "delta_pct": (
            round((latest - previous) / previous * 100, 4)
            if previous not in (None, 0)
            else None
        ),
    }


def _storage_payload() -> dict[str, str]:
    return {
        "database": str(Path(DB_PATH).resolve()),
        "table": CoalMonthlyPoint.__tablename__,
        "seed": str(_CSV.resolve()),
    }


def _slice_series(
    series: dict[str, list], days: int, *, full: bool
) -> tuple[list[str], dict[str, list]]:
    months: list[str] = series["months"]
    if full or not months:
        start = 0
    else:
        cutoff = (today_cn() - timedelta(days=days)).isoformat()[:7]
        start = next(
            (index for index, month in enumerate(months) if month >= cutoff),
            len(months) - 1,
        )
    window_months = months[start:]
    sliced = {
        key: values[start:] if isinstance(values, list) else values
        for key, values in series.items()
        if key != "months"
    }
    return window_months, sliced


def _payload(
    group_id: str,
    scope: str,
    window_months: list[str],
    metrics: list[dict[str, Any]],
    sliced: dict[str, list],
) -> dict[str, Any]:
    group = _require_group(group_id)
    return {
        "group": group.id,
        "group_label": group.label,
        "scope": scope,
        "as_of": window_months[-1] if window_months else None,
        "dates": window_months,
        "metrics": metrics,
        "series": sliced,
        "coverage": {
            "trading_days": len(window_months),
            "target": len(window_months),
            "complete": bool(window_months),
        },
        "storage": _storage_payload(),
        "source": {
            "name": "国家统计局",
            "url": _NBS_LIST,
            "cadence": "月度",
        },
    }


def build_payload(
    points: list[dict[str, Any]],
    days: int,
    *,
    full: bool = False,
) -> dict[str, Any]:
    series = _build_series(points)
    window_months, sliced = _slice_series(series, days, full=full)
    metrics = [
        metric
        for metric in (
            _metric(_OUTPUT_METRIC, window_months, sliced[_OUTPUT_METRIC], digits=0),
            _metric(_PER_TON_METRIC, window_months, sliced[_PER_TON_METRIC], digits=2),
            _metric(_PROFIT_METRIC, window_months, sliced[_PROFIT_METRIC], digits=2),
        )
        if metric is not None
    ]
    return _payload(
        COAL_MONTHLY_GROUP_ID,
        "原煤产量·吨煤利润（月度）",
        window_months,
        metrics,
        sliced,
    )


def build_industry_payload(
    points: list[dict[str, Any]],
    days: int,
    *,
    full: bool = False,
) -> dict[str, Any]:
    series = _build_series(points)
    window_months, sliced = _slice_series(series, days, full=full)
    metrics = [
        metric
        for metric in (
            _metric(_PROFIT_METRIC, window_months, sliced[_PROFIT_METRIC], digits=2),
            _metric(
                _POWER_PROFIT_METRIC,
                window_months,
                sliced[_POWER_PROFIT_METRIC],
                digits=2,
            ),
            _metric(
                _TOTAL_PROFIT_METRIC,
                window_months,
                sliced[_TOTAL_PROFIT_METRIC],
                digits=2,
            ),
        )
        if metric is not None
    ]
    return _payload(
        COAL_POWER_GROUP_ID,
        "煤炭与电力行业月度利润",
        window_months,
        metrics,
        sliced,
    )


def get_curve_payload(
    db: Session,
    group_id: str,
    days: int,
    *,
    force: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    _require_group(group_id)
    _ensure_seeded(db)
    if force:
        sync_from_nbs(db)
    points = load_points(db)
    if group_id == COAL_POWER_GROUP_ID:
        return build_industry_payload(points, days, full=full)
    return build_payload(points, days, full=full)
