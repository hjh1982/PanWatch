"""全球煤炭价格：三个数据源解析与双轴 payload 单元测试。"""

from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.modules.market import global_coal_service as service
from src.modules.market.etf_share_service import catalog_payload
from src.modules.market.statistics_store import StatisticsStore


def test_解析东财纽卡斯尔日线():
    payload = {
        "data": {
            "klines": [
                "2026-10-22,151.00,152.45,153.00,150.10,1200",
                "2026-10-23,152.45,154.45,155.00,152.00,1300",
                "2026-10-24,0,0,0,0,0",
                "bad-row",
            ]
        }
    }

    rows = service.parse_eastmoney_payload(payload)

    assert rows == {"2026-10-22": 152.45, "2026-10-23": 154.45}
    assert service.parse_eastmoney_payload({"data": None}) == {}
    assert service.parse_eastmoney_payload({"data": {"klines": "bad"}}) == {}


def test_解析CNBC鹿特丹日线():
    payload = {
        "barData": {
            "priceBars": [
                {"tradeTime": "20261019000000", "close": 146.4},
                {"tradeTime": "20261020", "close": "147.1"},
                {"tradeTime": "20261021", "close": 0},
                {"tradeTime": "bad", "close": 150},
            ]
        }
    }

    rows = service.parse_cnbc_payload(payload)

    assert rows == {"2026-10-19": 146.4, "2026-10-20": 147.1}
    assert service.parse_cnbc_payload({"barData": {}}) == {}


def test_解析新浪焦煤日线兼容JSONP与包装格式():
    raw = [
        {"d": "2026-10-22", "c": "1420.500"},
        {"d": "2026-10-23", "c": "1427.500"},
        {"d": "bad", "c": "1"},
    ]
    jsonp = "/*<script></script>*/\nvar _JM0=([{\"d\":\"2026-10-23\",\"c\":\"1427.500\"}]);"
    wrapped = {"result": {"status": {"code": 0}, "data": raw}}

    assert service.parse_sina_payload(raw) == {
        "2026-10-22": 1420.5,
        "2026-10-23": 1427.5,
    }
    assert service.parse_sina_payload(jsonp) == {"2026-10-23": 1427.5}
    assert service.parse_sina_payload(wrapped) == {
        "2026-10-22": 1420.5,
        "2026-10-23": 1427.5,
    }
    assert service.parse_sina_payload("var _JM0=(null);") == {}


def test_双轴payload包含三序列与来源信息():
    rows = {
        "newcastle": {"2026-10-20": 140.0, "2026-10-21": 150.0, "2026-10-22": 152.0},
        "rotterdam": {"2026-10-20": 144.0, "2026-10-21": 145.0, "2026-10-22": 146.5},
        "coking_coal": {"2026-10-22": 1427.5},
    }

    payload = service.build_payload(rows, 2)

    assert payload["group"] == "globalCoal"
    assert payload["dates"] == ["2026-10-21", "2026-10-22"]
    assert payload["series"] == {
        "newcastle": [150.0, 152.0],
        "rotterdam": [145.0, 146.5],
        "coking_coal": [None, 1427.5],
    }
    assert payload["as_of"] == "2026-10-22"
    assert payload["axes"] == {
        "left": {"label": "ICE 动力煤", "unit": "美元/吨"},
        "right": {"label": "焦煤期货", "unit": "元/吨"},
    }
    assert payload["coverage"] == {
        "trading_days": 2,
        "target": 2,
        "complete": True,
    }

    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert list(metrics) == ["newcastle", "rotterdam", "coking_coal"]
    assert metrics["newcastle"]["axis"] == "left"
    assert metrics["newcastle"]["unit"] == "美元/吨"
    assert metrics["newcastle"]["latest"] == pytest.approx(152.0)
    assert metrics["newcastle"]["delta"] == pytest.approx(2.0)
    assert metrics["newcastle"]["delta_pct"] == pytest.approx(1.3333, abs=1e-4)
    assert metrics["coking_coal"]["axis"] == "right"
    assert metrics["coking_coal"]["unit"] == "元/吨"
    assert metrics["coking_coal"]["delta"] is None

    sources = {source["id"]: source for source in payload["sources"]}
    assert sources["newcastle"]["start"] == "2026-10-20"
    assert sources["newcastle"]["latest"] == "2026-10-22"
    assert sources["coking_coal"]["start"] == "2026-10-22"
    assert sources["newcastle"]["cadence"] == "日线"


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
    assert all(source["start"] is None for source in payload["sources"])


def test_目录包含全球煤炭分组():
    catalog = catalog_payload()
    group = next(item for item in catalog["groups"] if item["id"] == "globalCoal")

    assert group["label"] == "全球主要煤炭价格"
    assert group["category"] == "commodity"
    assert group["available"] is True


def test_曲线接口分发到全球煤炭服务(monkeypatch):
    from src.modules.market.api import etf_shares as api

    monkeypatch.setattr(
        service,
        "load_rows",
        lambda force=False: {
            "newcastle": {"2026-10-22": 152.0},
            "rotterdam": {"2026-10-22": 146.5},
            "coking_coal": {"2026-10-22": 1427.5},
        },
    )

    payload = api._load_curve(None, "globalCoal", 30, force=False, full=False)

    assert payload["group"] == "globalCoal"
    assert payload["dates"] == ["2026-10-22"]
    assert len(payload["metrics"]) == 3


def _new_store():
    return StatisticsStore(
        namespace=service.GLOBAL_COAL_GROUP_ID, default_ttl_sec=service._CACHE_TTL_S
    )


def test_各序列写入后新实例读回且每次读取数据库(statistics_db, monkeypatch):
    rows = {
        "newcastle": {"2026-10-22": 152.0},
        "rotterdam": {"2026-10-22": 146.5},
        "coking_coal": {"2026-10-22": 1427.5},
    }
    fetchers = {key: Mock(return_value=value) for key, value in rows.items()}
    monkeypatch.setattr(service, "_FETCHERS", fetchers)
    assert service.load_rows(force=True) == rows

    restarted = _new_store()
    assert {key: restarted.get(key) for key in rows} == rows
    monkeypatch.setattr(service, "_store", restarted)
    for fetch in fetchers.values():
        fetch.reset_mock()
    loaded = service.load_rows()
    assert loaded == rows
    loaded["newcastle"]["2026-10-22"] = -1
    assert service.load_rows() == rows

    _new_store().set("newcastle", {"2026-10-22": 153.0})
    assert service.load_rows()["newcastle"] == {"2026-10-22": 153.0}
    for fetch in fetchers.values():
        fetch.assert_not_called()


@pytest.mark.parametrize("series_id", [item.id for item in service.SERIES])
@pytest.mark.parametrize("force", [False, True])
def test_到期或force合并缩窗响应且同日不重复(
    statistics_db, monkeypatch, series_id, force
):
    previous = {"2026-10-20": 140.0, "2026-10-22": 152.0, "2026-10-23": 154.0}
    incoming = {"2026-10-19": 139.0, "2026-10-22": 153.0, "2026-10-23": None}
    service._store.set(
        series_id, previous, ttl_sec=service._CACHE_TTL_S if force else -1
    )
    fetch = Mock(return_value=incoming)
    monkeypatch.setitem(service._FETCHERS, series_id, fetch)
    expected = {
        "2026-10-19": 139.0,
        "2026-10-20": 140.0,
        "2026-10-22": 153.0,
        "2026-10-23": 154.0,
    }

    assert service._load_series(series_id, force=force) == expected
    assert service._load_series(series_id, force=force) == expected
    assert _new_store().get(series_id) == expected
    assert list(service._load_series(series_id, force=False)) == list(expected)
    assert fetch.call_count == (2 if force else 1)
    assert previous["2026-10-22"] == 152.0
    assert incoming["2026-10-23"] is None


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("has_history", [False, True])
@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_空响应或外部异常回退各序列历史且不续期(
    statistics_db, monkeypatch, caplog, force, has_history, failure
):
    previous = {
        item.id: {"2026-10-22": 152.0} if has_history else {}
        for item in service.SERIES
    }
    if has_history:
        for key, value in previous.items():
            service._store.set(key, value, ttl_sec=-1)
    network = Mock(return_value=None)
    if failure == "exception":
        network.side_effect = httpx.ReadTimeout("coal unavailable")
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
        assert "coal unavailable" in caplog.text
        assert any(record.levelname == "WARNING" for record in caplog.records)


def test_部分系列失败保留历史且仅成功系列续期(statistics_db, monkeypatch):
    previous = {
        "newcastle": {"2026-10-21": 150.0},
        "rotterdam": {"2026-10-21": 146.5},
        "coking_coal": {"2026-10-21": 1427.5},
    }
    for key, value in previous.items():
        service._store.set(key, value, ttl_sec=-1)
    fetchers = {
        "newcastle": Mock(return_value={"2026-10-22": 152.0}),
        "rotterdam": Mock(return_value={}),
        "coking_coal": Mock(side_effect=httpx.ConnectError("sina unavailable")),
    }
    monkeypatch.setattr(service, "_FETCHERS", fetchers)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    write = Mock(wraps=service._store.set)
    monkeypatch.setattr(service._store, "set", write)
    expected = {**previous, "newcastle": {"2026-10-21": 150.0, "2026-10-22": 152.0}}

    assert service.load_rows() == expected
    write.assert_called_once_with("newcastle", expected["newcastle"])
    assert _new_store().get("newcastle") == expected["newcastle"]
    for key in ("rotterdam", "coking_coal"):
        assert _new_store().get(key) is None
        assert _new_store().get(key, allow_stale=True) == previous[key]


@pytest.mark.parametrize("series_id", [item.id for item in service.SERIES])
def test_数据库写入异常透过并发loader抛出(statistics_db, monkeypatch, series_id):
    for item in service.SERIES:
        if item.id != series_id:
            service._store.set(item.id, {"2026-10-22": 140.0})
    monkeypatch.setitem(
        service._FETCHERS, series_id, lambda: {"2026-10-22": 152.0}
    )
    monkeypatch.setattr(
        service._store, "set", Mock(side_effect=SQLAlchemyError("write failed"))
    )

    with pytest.raises(SQLAlchemyError, match="write failed"):
        service.load_rows()
