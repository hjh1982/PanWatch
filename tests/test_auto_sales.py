"""中国乘用车产销：乘联会解析、同比 payload 与目录分发单元测试。"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.modules.market import auto_sales_service as service
from src.modules.market.etf_share_service import catalog_payload
from src.modules.market.statistics_store import StatisticsStore


def _entry(month: str, **years: list[float]) -> dict:
    return {"month": month, **{f"{year}年": values for year, values in years.items()}}


def test_解析乘联会接口按总量区分狭义广义():
    payload = [
        {
            "category": "整体市场",
            "dataList": [
                _entry("1月", **{"2026": [100.0, 90.0, 80.0, 10.0], "2025": [110.0, 100.0, 95.0, 5.0]}),
                _entry("9月", **{"2026": [120.0, 115.0, 105.0, 30.0]}),
                _entry("12月", **{"2025": [130.0, 128.0, 120.0, 6.0]}),
            ],
        },
        {
            "category": "整体市场",
            "dataList": [
                _entry("1月", **{"2026": [102.0, 92.0, 82.0, 11.0], "2025": [112.0, 102.0, 97.0, 6.0]}),
                _entry("9月", **{"2026": [122.0, 117.0, 107.0, 31.0]}),
            ],
        },
    ]

    rows = service.parse_cpca_payload(payload)

    assert list(rows) == ["2025-01", "2025-12", "2026-01", "2026-09"]
    assert rows["2026-09"] == {
        "narrow_production": 120.0,
        "narrow_wholesale": 115.0,
        "narrow_retail": 105.0,
        "narrow_export": 30.0,
        "wide_production": 122.0,
        "wide_wholesale": 117.0,
        "wide_retail": 107.0,
        "wide_export": 31.0,
    }
    assert rows["2025-12"] == {
        "narrow_production": 130.0,
        "narrow_wholesale": 128.0,
        "narrow_retail": 120.0,
        "narrow_export": 6.0,
    }
    assert service.parse_cpca_payload([]) == {}
    assert service.parse_cpca_payload({"data": []}) == {}
    assert service.parse_cpca_payload(payload[:1]) == {}


def test_两组数据顺序颠倒仍按总量识别狭义广义():
    narrow = {"dataList": [_entry("1月", **{"2026": [100.0, 90.0, 80.0, 10.0]})]}
    wide = {"dataList": [_entry("1月", **{"2026": [120.0, 110.0, 100.0, 12.0]})]}

    rows = service.parse_cpca_payload([wide, narrow])

    assert rows["2026-01"]["narrow_production"] == 100.0
    assert rows["2026-01"]["wide_production"] == 120.0


def test_payload按年给出同比指标与对齐序列():
    rows = {
        "2025-09": {
            "narrow_production": 284.6384,
            "narrow_wholesale": 279.6051,
            "narrow_retail": 224.3658,
            "narrow_export": 52.8082,
            "wide_production": 288.0568,
            "wide_wholesale": 282.8011,
            "wide_retail": 226.8751,
            "wide_export": 53.3046,
        },
        "2026-08": {
            "narrow_production": 234.8322,
            "narrow_wholesale": 235.7542,
            "narrow_retail": 154.1478,
            "narrow_export": 88.8509,
            "wide_production": 236.1357,
            "wide_wholesale": 237.5427,
            "wide_retail": 155.4402,
            "wide_export": 89.5312,
        },
        "2026-09": {
            "narrow_production": 259.0388,
            "narrow_wholesale": 254.9411,
            "narrow_retail": 161.7989,
            "narrow_export": 91.8881,
            "wide_production": 260.61,
            "wide_wholesale": 256.6616,
            "wide_retail": 163.1079,
            "wide_export": 92.4888,
        },
    }

    payload = service.build_payload(rows, 90)

    assert payload["group"] == "autoSales"
    assert payload["dates"] == ["2025-09", "2026-08", "2026-09"]
    assert payload["as_of"] == "2026-09"
    assert payload["series"]["narrow_production"] == [284.6384, 234.8322, 259.0388]
    assert payload["series"]["wide_export"] == [53.3046, 89.5312, 92.4888]
    assert payload["coverage"] == {
        "trading_days": 3,
        "target": 3,
        "complete": True,
    }
    assert payload["source"]["name"] == "乘联会（CPCA）"
    assert payload["source"]["cadence"] == "月度"

    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert list(metrics) == ["production", "wholesale", "retail", "export"]
    assert metrics["production"]["label"] == "产量"
    assert metrics["production"]["unit"] == "万辆"
    assert metrics["production"]["latest"] == pytest.approx(259.0388)
    assert metrics["production"]["prior"] == pytest.approx(284.6384)
    assert metrics["production"]["delta"] == pytest.approx(-25.5996)
    assert metrics["production"]["delta_pct"] == pytest.approx(-8.9937, abs=1e-3)
    assert metrics["export"]["delta_pct"] == pytest.approx(74.0035, abs=1e-3)


def test_缺少去年同月时同比为空但不报错():
    rows = {
        "2026-09": {
            "narrow_production": 259.0388,
            "narrow_wholesale": 254.9411,
            "narrow_retail": 161.7989,
            "narrow_export": 91.8881,
            "wide_production": 260.61,
            "wide_wholesale": 256.6616,
            "wide_retail": 163.1079,
            "wide_export": 92.4888,
        }
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


def test_目录包含乘用车产销分组():
    catalog = catalog_payload()
    group = next(item for item in catalog["groups"] if item["id"] == "autoSales")

    assert group["label"] == "中国乘用车产销"
    assert group["category"] == "industry"
    assert group["available"] is True


def test_曲线接口分发到乘用车产销服务(monkeypatch):
    from src.modules.market.api import etf_shares as api

    monkeypatch.setattr(
        service,
        "load_rows",
        lambda force=False: {
            "2026-09": {
                "narrow_production": 259.0388,
                "narrow_wholesale": 254.9411,
                "narrow_retail": 161.7989,
                "narrow_export": 91.8881,
                "wide_production": 260.61,
                "wide_wholesale": 256.6616,
                "wide_retail": 163.1079,
                "wide_export": 92.4888,
            }
        },
    )

    payload = api._load_curve(None, "autoSales", 30, force=False, full=False)

    assert payload["group"] == "autoSales"
    assert payload["dates"] == ["2026-09"]
    assert len(payload["metrics"]) == 4


def _new_store():
    return StatisticsStore(
        namespace=service.AUTO_SALES_GROUP_ID, default_ttl_sec=service._CACHE_TTL_S
    )


def test_写入后新实例读回且每次读取数据库(statistics_db, monkeypatch):
    rows = {"2026-09": {"narrow_production": 259.0}}
    fetch = Mock(return_value=rows)
    monkeypatch.setattr(service, "_fetch_rows", fetch)
    assert service.load_rows(force=True) == rows

    restarted = _new_store()
    assert restarted.get("rows") == rows
    monkeypatch.setattr(service, "_store", restarted)
    fetch.reset_mock()
    loaded = service.load_rows()
    assert loaded == rows
    loaded["2026-09"]["narrow_production"] = -1
    assert service.load_rows() == rows

    changed = {"2026-09": {"narrow_production": 260.0}}
    _new_store().set("rows", changed)
    assert service.load_rows() == changed
    fetch.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
def test_到期或force合并缩窗及缺失字段且同月不重复(
    statistics_db, monkeypatch, force
):
    previous = {
        "2025-09": {"narrow_production": 250.0},
        "2026-09": {"narrow_production": 259.0, "wide_production": 261.0},
    }
    incoming = {
        "2026-08": {"narrow_production": 230.0},
        "2026-09": {"narrow_production": 260.0, "wide_production": None},
    }
    original = deepcopy(incoming)
    service._store.set(
        "rows", previous, ttl_sec=service._CACHE_TTL_S if force else -1
    )
    fetch = Mock(return_value=incoming)
    monkeypatch.setattr(service, "_fetch_rows", fetch)
    expected = {
        "2025-09": {"narrow_production": 250.0},
        "2026-08": {"narrow_production": 230.0},
        "2026-09": {"narrow_production": 260.0, "wide_production": 261.0},
    }

    assert service.load_rows(force=force) == expected
    assert service.load_rows(force=force) == expected
    assert _new_store().get("rows") == expected
    assert list(service.load_rows()) == list(expected)
    assert fetch.call_count == (2 if force else 1)
    assert incoming == original
    assert previous["2026-09"]["narrow_production"] == 259.0


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("has_history", [False, True])
@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_空响应或外部异常回退历史且不续期(
    statistics_db, monkeypatch, caplog, force, has_history, failure
):
    previous = {"2025-09": {"narrow_production": 250.0}} if has_history else {}
    if has_history:
        service._store.set("rows", previous, ttl_sec=-1)
    network = Mock(return_value=None)
    if failure == "exception":
        network.side_effect = httpx.ReadTimeout("CPCA unavailable")
    monkeypatch.setattr(service, "market_get", network)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    write = Mock(wraps=service._store.set)
    monkeypatch.setattr(service._store, "set", write)

    assert service.load_rows(force=force) == previous
    assert _new_store().get("rows", allow_stale=True) == (previous if has_history else None)
    assert _new_store().get("rows") is None
    write.assert_not_called()
    assert network.called
    if failure == "exception":
        assert "CPCA unavailable" in caplog.text
        assert any(record.levelname == "WARNING" for record in caplog.records)


def test_数据库写入异常不被网络回退吞掉(statistics_db, monkeypatch):
    monkeypatch.setattr(
        service, "_fetch_rows", lambda: {"2026-09": {"narrow_production": 259.0}}
    )
    monkeypatch.setattr(
        service._store, "set", Mock(side_effect=SQLAlchemyError("write failed"))
    )

    with pytest.raises(SQLAlchemyError, match="write failed"):
        service.load_rows(force=True)
