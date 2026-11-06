"""美国 M2 vs 黄金：数据解析、payload 对齐与目录分发单元测试。"""

from __future__ import annotations

import io
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError
from openpyxl import Workbook

from src.modules.market import us_m2_gold_service as service
from src.modules.market.etf_share_service import catalog_payload
from src.modules.market.statistics_store import StatisticsStore


def _m2_payload(periods: list[str], values: list[object]) -> dict:
    return {
        "series": {
            "docs": [
                {
                    "series_name": "Monthly – Seasonally adjusted",
                    "period": periods,
                    "value": values,
                }
            ]
        }
    }


def _gold_workbook(rows: list[tuple[object, object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Monthly Prices"
    sheet.append(["World Bank Commodity Price Data"])
    sheet.append([])
    sheet.append(["Updated on October 02, 2026"])
    sheet.append(["", "Crude oil, average", "Gold"])
    sheet.append(["", "($/bbl)", "($/troy oz)"])
    for month, value in rows:
        sheet.append([month, 80.0, value])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_解析M2月度序列并折算为万亿美元():
    payload = _m2_payload(
        ["1959-01-31", "1959-02-28", "2026-08-31", "2026-09-30"],
        [286.6, 287.7, None, 23413.9],
    )

    rows = service.parse_m2_payload(payload)

    assert list(rows) == ["1959-01", "1959-02", "2026-09"]
    assert rows["1959-01"] == pytest.approx(0.2866)
    assert rows["2026-09"] == pytest.approx(23.4139)
    assert service.parse_m2_payload({}) == {}
    assert service.parse_m2_payload({"series": {"docs": []}}) == {}


def test_解析世界银行金价表并跳过缺失月份():
    content = _gold_workbook(
        [
            (1960 + 0, None),
            ("1960M01", 35.0),
            ("1960M02", "35.25"),
            ("1960M03", "…"),
            ("2026M09", 4319.0),
        ]
    )

    rows = service.parse_wb_gold_workbook(content)

    assert list(rows) == ["1960-01", "1960-02", "2026-09"]
    assert rows["1960-01"] == pytest.approx(35.0)
    assert rows["2026-09"] == pytest.approx(4319.0)
    assert service.parse_wb_gold_workbook(b"") == {}
    assert service.parse_wb_gold_workbook(b"not-an-xlsx") == {}


def test_从世界银行页面提取当期价格表链接():
    html = (
        '<a href="https://thedocs.worldbank.org/en/doc/abc-0050012026/'
        'related/CMO-Historical-Data-Monthly.xlsx">monthly</a>'
    )

    assert (
        service.parse_wb_gold_link(html)
        == "https://thedocs.worldbank.org/en/doc/abc-0050012026/"
        "related/CMO-Historical-Data-Monthly.xlsx"
    )
    assert service.parse_wb_gold_link("<html></html>") is None
    assert service.parse_wb_gold_link(None) is None


def test_payload按自然月对齐并给出同比指标():
    rows = {
        "m2": {
            "1959-01": 286.6,
            "1960-01": 0.31,
            "2025-09": 21.0,
            "2026-09": 23.4139,
        },
        "gold": {
            "1960-01": 35.0,
            "2025-09": 3500.0,
            "2025-10": 3600.0,
            "2026-09": 4319.0,
            "2026-10": 4400.0,
        },
    }

    payload = service.build_payload(rows, 90)

    assert payload["group"] == "usM2Gold"
    assert payload["dates"] == [
        "1960-01",
        "2025-09",
        "2025-10",
        "2026-09",
        "2026-10",
    ]
    assert payload["series"]["m2"] == [0.31, 21.0, None, 23.4139, None]
    assert payload["series"]["gold"] == [35.0, 3500.0, 3600.0, 4319.0, 4400.0]
    assert payload["as_of"] == "2026-09"
    assert payload["coverage"] == {
        "trading_days": 3,
        "target": 5,
        "complete": False,
    }
    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert metrics["m2"]["axis"] == "left"
    assert metrics["m2"]["unit"] == "万亿美元"
    assert metrics["m2"]["latest"] == pytest.approx(23.4139)
    assert metrics["m2"]["prior"] == pytest.approx(21.0)
    assert metrics["m2"]["delta_pct"] == pytest.approx(11.4948, abs=1e-3)
    assert metrics["gold"]["axis"] == "right"
    assert metrics["gold"]["unit"] == "美元/盎司"
    assert metrics["gold"]["latest"] == pytest.approx(4400.0)
    assert metrics["gold"]["latest_date"] == "2026-10"
    assert metrics["gold"]["prior"] == pytest.approx(3600.0)
    assert metrics["gold"]["delta_pct"] == pytest.approx(22.2222, abs=1e-3)
    sources = {source["id"]: source for source in payload["sources"]}
    assert sources["m2"]["start"] == "1959-01"
    assert sources["m2"]["latest"] == "2026-09"
    assert sources["gold"]["latest"] == "2026-10"


def test_payload缺失同比月份时不补值():
    rows = {
        "m2": {"2026-09": 23.4139},
        "gold": {"2026-09": 4319.0},
    }

    payload = service.build_payload(rows, 90)

    assert payload["dates"] == ["2026-09"]
    for metric in payload["metrics"]:
        assert metric["prior"] is None
        assert metric["delta"] is None
        assert metric["delta_pct"] is None


def test_全部序列缺失时payload为空但不报错():
    payload = service.build_payload({}, 30)

    assert payload["dates"] == []
    assert payload["metrics"] == []
    assert payload["as_of"] is None
    assert payload["coverage"] == {
        "trading_days": 0,
        "target": 0,
        "complete": False,
    }


def test_目录包含M2与黄金分组():
    catalog = catalog_payload()
    group = next(item for item in catalog["groups"] if item["id"] == "usM2Gold")

    assert group["label"] == "美国 M2 vs 黄金"
    assert group["category"] == "macro"
    assert group["available"] is True


def test_曲线接口分发到M2与黄金服务(monkeypatch):
    from src.modules.market.api import etf_shares as api

    monkeypatch.setattr(
        service,
        "load_rows",
        lambda force=False: {
            "m2": {"2026-09": 23.4139},
            "gold": {"2026-09": 4319.0},
        },
    )

    payload = api._load_curve(None, "usM2Gold", 30, force=False, full=False)

    assert payload["group"] == "usM2Gold"
    assert payload["dates"] == ["2026-09"]
    assert len(payload["metrics"]) == 2


def _new_store():
    return StatisticsStore(
        namespace=service.US_M2_GOLD_GROUP_ID, default_ttl_sec=service._CACHE_TTL_S
    )


def test_两源分别写入后新实例读回且每次读取数据库(statistics_db, monkeypatch):
    rows = {"m2": {"2026-09": 23.4}, "gold": {"2026-09": 4319.0}}
    fetchers = {key: Mock(return_value=value) for key, value in rows.items()}
    for key, fetch in fetchers.items():
        monkeypatch.setattr(service, f"_fetch_{key}", fetch)
    assert service.load_rows(force=True) == rows

    restarted = _new_store()
    assert restarted.get("rows", allow_stale=True) is None
    assert {key: restarted.get(key) for key in rows} == rows
    monkeypatch.setattr(service, "_store", restarted)
    for fetch in fetchers.values():
        fetch.reset_mock()
    loaded = service.load_rows()
    assert loaded == rows
    loaded["m2"]["2026-09"] = -1
    assert service.load_rows() == rows

    _new_store().set("m2", {"2026-09": 23.5})
    assert service.load_rows()["m2"] == {"2026-09": 23.5}
    for fetch in fetchers.values():
        fetch.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
def test_到期或force分别合并缩窗响应且同月不重复(statistics_db, monkeypatch, force):
    previous = {
        "m2": {"1960-01": 0.3, "2026-09": 23.4, "2026-10": 23.5},
        "gold": {"1960-01": 35.0, "2026-09": 4319.0, "2026-10": 4400.0},
    }
    incoming = {
        "m2": {"1959-01": 0.28, "2026-09": 23.41, "2026-10": None},
        "gold": {"2026-09": 4320.0, "2026-10": None},
    }
    fetchers = {key: Mock(return_value=value) for key, value in incoming.items()}
    for key, value in previous.items():
        service._store.set(key, value, ttl_sec=service._CACHE_TTL_S if force else -1)
        monkeypatch.setattr(service, f"_fetch_{key}", fetchers[key])
    expected = {
        "m2": {"1959-01": 0.28, "1960-01": 0.3, "2026-09": 23.41, "2026-10": 23.5},
        "gold": {"1960-01": 35.0, "2026-09": 4320.0, "2026-10": 4400.0},
    }

    assert service.load_rows(force=force) == expected
    assert service.load_rows(force=force) == expected
    for key, value in expected.items():
        assert _new_store().get(key) == value
        assert list(service.load_rows()[key]) == list(value)
        assert fetchers[key].call_count == (2 if force else 1)
        assert incoming[key]["2026-10"] is None
    assert previous["m2"]["2026-09"] == 23.4
    assert previous["gold"]["2026-09"] == 4319.0


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("has_history", [False, True])
@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_两源空响应或外部异常回退历史且不续期(
    statistics_db, monkeypatch, caplog, force, has_history, failure
):
    previous = {
        "m2": {"2026-09": 23.4} if has_history else {},
        "gold": {"2026-09": 4319.0} if has_history else {},
    }
    if has_history:
        for key, value in previous.items():
            service._store.set(key, value, ttl_sec=-1)
    network = Mock(return_value=None)
    if failure == "exception":
        network.side_effect = httpx.ReadTimeout("source unavailable")
    monkeypatch.setattr(service, "market_get", network)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    write = Mock(wraps=service._store.set)
    monkeypatch.setattr(service._store, "set", write)

    assert service.load_rows(force=force) == previous
    restarted = _new_store()
    for key, value in previous.items():
        assert restarted.get(key, allow_stale=True) == (value if has_history else None)
        assert restarted.get(key) is None
    write.assert_not_called()
    assert network.called
    if failure == "exception":
        assert "source unavailable" in caplog.text
        assert any(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.parametrize("failed_key", ["m2", "gold"])
@pytest.mark.parametrize("failure", ["empty", "exception"])
@pytest.mark.parametrize("force", [False, True])
def test_单源失败不随成功源续期且下次只刷新失败源(
    statistics_db, monkeypatch, failed_key, failure, force
):
    previous = {"m2": {"2026-08": 23.0}, "gold": {"2026-08": 4200.0}}
    updates = {"m2": {"2026-09": 23.4}, "gold": {"2026-09": 4319.0}}
    successful_key = "gold" if failed_key == "m2" else "m2"
    fetchers = {key: Mock(return_value=value) for key, value in updates.items()}
    fetchers[failed_key].return_value = {}
    if failure == "exception":
        fetchers[failed_key].side_effect = httpx.ConnectError("source unavailable")
    for key, value in previous.items():
        service._store.set(key, value, ttl_sec=-1)
        monkeypatch.setattr(service, f"_fetch_{key}", fetchers[key])
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    write = Mock(wraps=service._store.set)
    monkeypatch.setattr(service._store, "set", write)
    expected = {
        **previous,
        successful_key: {**previous[successful_key], **updates[successful_key]},
    }

    assert service.load_rows(force=force) == expected
    write.assert_called_once_with(successful_key, expected[successful_key])
    restarted = _new_store()
    assert restarted.get(successful_key) == expected[successful_key]
    assert restarted.get(failed_key) is None
    assert restarted.get(failed_key, allow_stale=True) == previous[failed_key]

    # 成功源仍新鲜；失败源必须再次抓取，而不是共享一个快照的 TTL。
    write.reset_mock()
    for fetch in fetchers.values():
        fetch.reset_mock()
    fetchers[failed_key].side_effect = None
    fetchers[failed_key].return_value = updates[failed_key]
    recovered = {key: {**previous[key], **updates[key]} for key in previous}
    assert service.load_rows() == recovered
    fetchers[successful_key].assert_not_called()
    fetchers[failed_key].assert_called_once_with()
    write.assert_called_once_with(failed_key, recovered[failed_key])
    assert {key: _new_store().get(key) for key in previous} == recovered


@pytest.mark.parametrize("series_id", ["m2", "gold"])
def test_数据库写入异常不被网络回退吞掉(statistics_db, monkeypatch, series_id):
    other = "gold" if series_id == "m2" else "m2"
    service._store.set(other, {"2026-09": 100.0})
    monkeypatch.setattr(service, f"_fetch_{series_id}", lambda: {"2026-09": 200.0})
    monkeypatch.setattr(
        service._store, "set", Mock(side_effect=SQLAlchemyError("write failed"))
    )

    with pytest.raises(SQLAlchemyError, match="write failed"):
        service.load_rows()
