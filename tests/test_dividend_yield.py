"""A 股股息率：申万/国债解析、对齐、payload 与接口转发。"""

from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.market import dividend_yield_service as service
from src.modules.market.api import etf_shares as etf_shares_api
from src.platform.persistence.database import get_db


def test_申万响应解析日期与股息率():
    payload = {
        "data": {
            "results": [
                {"bargaindate": "2026-10-30T08:00:00+08:00", "dp": "1.84"},
                {"bargaindate": "2026-10-29", "dp": 1.8},
                {"bargaindate": "", "dp": "1.5"},
                {"bargaindate": "2026-10-28", "dp": "-0.1"},
                {"bargaindate": "2026-10-27", "dp": "bad"},
            ]
        }
    }

    assert service.parse_sw_rows(payload) == {
        "2026-10-30": 1.84,
        "2026-10-29": 1.8,
    }
    assert service.parse_sw_rows(None) == {}


def test_国债响应只取十年期字段():
    payload = {
        "result": {
            "data": [
                {
                    "SOLAR_DATE": "2026-10-30 00:00:00",
                    "EMM00166466": 1.65,
                    "EMM00166462": 1.35,
                },
                {
                    "SOLAR_DATE": "2026-10-29 00:00:00",
                    "EMM00166466": None,
                },
            ]
        }
    }

    assert service.parse_bond_rows(payload) == {"2026-10-30": 1.65}
    assert service.parse_bond_rows({}) == {}


def test_国债收益率按最近公布值向后填充():
    dates = ["2026-10-27", "2026-10-29", "2026-10-30"]
    bond = {"2026-10-28": 1.66, "2026-10-30": 1.65}

    assert service._align_bond(dates, bond) == [None, 1.66, 1.65]


def test_payload对齐序列并计算利差与分位():
    equity = {
        "all": {
            "2026-09-30": 1.7,
            "2026-10-29": 1.8,
            "2026-10-30": 1.84,
        },
        "large": {
            "2026-09-30": 2.5,
            "2026-10-29": 2.6,
            "2026-10-30": 2.62,
        },
    }
    bond = {"2026-09-30": 1.7, "2026-10-30": 1.65}

    payload = service.build_payload(equity, bond)

    assert payload["group"] == "dividendYield"
    assert payload["dates"] == ["2026-09-30", "2026-10-29", "2026-10-30"]
    assert payload["as_of"] == "2026-10-30"
    assert payload["coverage"] == {
        "trading_days": 3,
        "target": 3,
        "complete": True,
    }
    assert payload["series"]["all"] == [1.7, 1.8, 1.84]
    assert payload["series"]["bond10y"] == [1.7, 1.7, 1.65]
    assert payload["series"]["spread"] == [0.0, 0.1, 0.19]
    assert "mid" not in payload["series"]

    metrics = {item["id"]: item for item in payload["metrics"]}
    assert metrics["all"]["latest"] == 1.84
    assert metrics["all"]["prior"] == 1.7
    assert metrics["all"]["delta"] == 0.14
    assert metrics["all"]["delta_pct"] == 8.2353
    assert metrics["all"]["percentile"] == 100.0
    assert metrics["bond10y"]["delta"] == -0.05
    assert metrics["bond10y"]["percentile"] == 33.3
    assert metrics["spread"]["unit"] == "pp"
    assert metrics["spread"]["delta"] == 0.19
    assert metrics["spread"]["delta_pct"] is None
    assert "mid" not in metrics

    assert payload["sources"][0]["start"] == "2026-09-30"
    assert payload["sources"][1]["latest"] == "2026-10-30"


def test_没有国债数据时只返回股息率():
    equity = {"all": {"2026-10-30": 1.84}}
    payload = service.build_payload(equity, {})

    assert payload["series"] == {"all": [1.84]}
    assert [item["id"] for item in payload["metrics"]] == ["all"]
    assert payload["sources"][1]["start"] is None


def test_空数据payload():
    payload = service.build_payload({}, {"2026-10-30": 1.65})

    assert payload["dates"] == []
    assert payload["metrics"] == []
    assert payload["series"] == {}
    assert payload["as_of"] is None
    assert payload["coverage"]["complete"] is True


def test_曲线接口转发股息率分组(monkeypatch):
    calls = []

    def fake_payload(group_id, days, *, force=False):
        calls.append((group_id, days, force))
        return {"group": group_id, "dates": [], "metrics": [], "series": {}}

    monkeypatch.setattr(service, "get_curve_payload", fake_payload)
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: None

    response = TestClient(app).get(
        "/api/etf-shares/curve?group=dividendYield&days=90"
    )
    refresh = TestClient(app).post(
        "/api/etf-shares/refresh?group=dividendYield&days=90"
    )

    assert response.status_code == 200
    assert refresh.status_code == 200
    assert calls == [
        ("dividendYield", 90, False),
        ("dividendYield", 90, True),
    ]


def test_数据库持久化可由新Store实例直接读取(monkeypatch, statistics_db):
    equity = {"2026-10-29": 1.8, "2026-10-30": 1.84}
    bond = {"2026-10-29": 1.66, "2026-10-30": 1.65}
    fetch_equity = Mock(return_value=equity)
    fetch_bond = Mock(return_value=bond)
    monkeypatch.setattr(service, "_fetch_sw_index", fetch_equity)
    monkeypatch.setattr(service, "_fetch_bond_rows", fetch_bond)

    expected = service.load_rows(force=True)
    store = service.StatisticsStore(
        namespace=service.DIVIDEND_YIELD_GROUP_ID,
        default_ttl_sec=service._CACHE_TTL_S,
    )
    for key, _, _ in service._SW_INDEXES:
        assert store.get(f"equity:{key}") == equity
    assert store.get("bond") == bond
    monkeypatch.setattr(service, "_store", store)
    fetch_equity.reset_mock()
    fetch_bond.reset_mock()

    assert service.load_rows() == expected
    fetch_equity.assert_not_called()
    fetch_bond.assert_not_called()
    assert set(expected) == {"equity", "bond", "start", "end"}


def test_强制更新同日数值并保留各源日期历史(monkeypatch, statistics_db):
    previous = {"2026-10-29": 1.8, "2026-10-30": 1.84}
    for key, _, _ in service._SW_INDEXES:
        service._store.set(f"equity:{key}", previous)
    service._store.set("bond", {"2026-10-29": 1.66, "2026-10-30": 1.65})
    fetch_equity = Mock(return_value={"2026-10-30": 1.9, "2026-11-02": 1.92})
    fetch_bond = Mock(return_value={"2026-10-30": 1.6})
    monkeypatch.setattr(service, "_fetch_sw_index", fetch_equity)
    monkeypatch.setattr(service, "_fetch_bond_rows", fetch_bond)

    result = service.load_rows(force=True)

    expected = {"2026-10-29": 1.8, "2026-10-30": 1.9, "2026-11-02": 1.92}
    for key, _, _ in service._SW_INDEXES:
        assert result["equity"][key] == expected
        assert service._store.get(f"equity:{key}") == expected
    assert result["bond"] == {"2026-10-29": 1.66, "2026-10-30": 1.6}
    assert service._store.get("bond") == result["bond"]
    assert fetch_equity.call_count == 4
    fetch_bond.assert_called_once()


@pytest.mark.parametrize(
    "failed_key", ["equity:all", "equity:large", "equity:mid", "equity:small", "bond"]
)
@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_单源失败保留旧数据且其它成功源不延长其TTL(
    monkeypatch, statistics_db, caplog, failed_key, failure
):
    keys = [f"equity:{key}" for key, _, _ in service._SW_INDEXES] + ["bond"]
    previous = {"2026-10-29": 1.8}
    incoming = {"2026-10-30": 1.9}
    for key in keys:
        service._store.set(key, previous, ttl_sec=-1)
    calls = []

    def fetch(key):
        calls.append(key)
        if key == failed_key:
            if failure == "exception":
                raise RuntimeError("network down")
            return {}
        return incoming

    index_keys = {code: f"equity:{key}" for key, code, _ in service._SW_INDEXES}
    monkeypatch.setattr(
        service, "_fetch_sw_index",
        lambda index_type, code, start, end: fetch(index_keys[code]),
    )
    monkeypatch.setattr(service, "_fetch_bond_rows", lambda start, end: fetch("bond"))

    result = service.load_rows()

    assert calls == keys
    for key in keys:
        expected = previous if key == failed_key else {**previous, **incoming}
        actual = result["bond"] if key == "bond" else result["equity"][key.split(":")[1]]
        assert actual == expected
        assert service._store.get(key, allow_stale=True) == expected
        assert service._store.get(key) == (None if key == failed_key else expected)
    calls.clear()
    assert service.load_rows() == result
    assert calls == [failed_key]
    if failure == "exception":
        assert "network down" in caplog.text
        assert any(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_全源空响应或异常不清除本地历史(monkeypatch, statistics_db, force, failure):
    keys = [f"equity:{key}" for key, _, _ in service._SW_INDEXES] + ["bond"]
    previous = {"2026-10-30": 1.8}
    for key in keys:
        service._store.set(key, previous, ttl_sec=-1)
    fetch = Mock(return_value={})
    if failure == "exception":
        fetch.side_effect = RuntimeError("network down")
    monkeypatch.setattr(service, "_fetch_sw_index", fetch)
    monkeypatch.setattr(service, "_fetch_bond_rows", fetch)

    result = service.load_rows(force=force)

    assert result["equity"] == {key: previous for key, _, _ in service._SW_INDEXES}
    assert result["bond"] == previous
    assert fetch.call_count == 5
    for key in keys:
        assert service._store.get(key) is None
        assert service._store.get(key, allow_stale=True) == previous


@pytest.mark.parametrize("operation", ["get", "set"])
def test_数据库读写异常向上传播(monkeypatch, statistics_db, operation):
    monkeypatch.setattr(service, "_fetch_sw_index", Mock(return_value={"2026-10-30": 1.8}))
    monkeypatch.setattr(service, "_fetch_bond_rows", Mock(return_value={"2026-10-30": 1.6}))
    monkeypatch.setattr(
        service._store, operation, Mock(side_effect=RuntimeError("sqlite unavailable"))
    )

    with pytest.raises(RuntimeError, match="sqlite unavailable"):
        service.load_rows(force=True)
