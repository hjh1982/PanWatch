"""融资融券市场情绪：全市场历史解析、缓存与路由分发测试。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.market import margin_service
from src.modules.market.api import etf_shares as etf_shares_api
from src.platform.persistence.database import get_db

_ROWS = [
    {
        "DIM_DATE": "2026-10-21 00:00:00",
        "RZYE": 2_450_277_179_446,
        "RZMRE": 117_341_770_922,
        "RZCHE": 118_309_529_464,
        "RQYL": 4_425_081_478,
        "RQYE": 28_854_097_703,
        "RZRQYE": 2_479_131_277_149,
    },
    {
        "DIM_DATE": "2026-10-20 00:00:00",
        "RZYE": 2_451_244_937_988,
        "RZMRE": 121_023_885_098,
        "RZCHE": 123_153_187_027,
        "RQYL": 4_391_407_331,
        "RQYE": 29_101_691_183,
        "RZRQYE": 2_480_346_629_171,
    },
    {
        "DIM_DATE": "2026-10-19 00:00:00",
        "RZYE": 2_453_374_239_917,
        "RZMRE": 118_238_604_318,
        "RZCHE": 124_569_959_994,
        "RQYE": 29_167_627_323,
        "RZRQYE": 2_482_541_867_240,
    },
]


def test_全市场融资融券按交易日对齐并计算环比(monkeypatch):
    monkeypatch.setattr(margin_service, "today_cn", lambda: date(2026, 10, 22))
    targets = ["2026-10-19", "2026-10-20", "2026-10-21"]

    rows = margin_service.parse_market_payload({"result": {"data": _ROWS}})
    payload = margin_service.build_market_payload(rows, targets)

    assert payload["group"] == "margin"
    assert payload["group_label"] == "融资融券"
    assert payload["scope"] == "沪深两市"
    assert payload["dates"] == targets
    assert payload["as_of"] == "2026-10-21"
    assert payload["coverage"] == {
        "trading_days": 3,
        "target": 3,
        "complete": True,
    }
    assert [metric["id"] for metric in payload["metrics"]] == [
        "rz_balance",
        "rz_buy",
        "rz_repay",
        "rq_volume",
        "rq_balance",
        "total_balance",
    ]
    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert metrics["rz_balance"]["latest"] == pytest.approx(24502.771794, abs=1e-6)
    assert metrics["rz_balance"]["delta"] == pytest.approx(-9.677585, abs=1e-6)
    assert metrics["rz_balance"]["delta_pct"] == pytest.approx(-0.0395, abs=1e-4)
    assert metrics["rq_volume"]["latest"] == pytest.approx(44.250815, abs=1e-6)
    assert metrics["rq_volume"]["delta"] == pytest.approx(0.336741, abs=1e-6)
    assert payload["series"]["rq_volume"] == [None, 43.914073, 44.250815]
    assert payload["series"]["rz_buy"] == [1182.386043, 1210.238851, 1173.417709]
    assert payload["series"]["total_balance"] == [
        24825.418672,
        24803.466292,
        24791.312771,
    ]


def test_全市场报表走东财接口并按缓存复用(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_market_get(url, **kwargs):
        calls.append((url, kwargs))
        return {"result": {"data": _ROWS}}

    monkeypatch.setattr(margin_service, "market_get", fake_market_get)
    monkeypatch.setattr(
        margin_service,
        "target_trade_dates",
        lambda days: ["2026-10-21"],
    )
    monkeypatch.setattr(margin_service, "today_cn", lambda: date(2026, 10, 22))

    first = margin_service.get_curve_payload("margin", 30)
    second = margin_service.get_curve_payload("margin", 30)
    margin_service.get_curve_payload("margin", 30, force=True)

    assert len(calls) == 2
    url, kwargs = calls[0]
    assert url == margin_service._DATACENTER_URL
    assert kwargs["params"]["reportName"] == "RPTA_RZRQ_LSHJ"
    assert kwargs["params"]["sortColumns"] == "DIM_DATE"
    assert kwargs["params"]["sortTypes"] == "-1"
    assert kwargs["parse"] == "json"
    assert first == second
    assert first["metrics"][0]["latest"] == pytest.approx(24502.771794, abs=1e-6)
    assert first["series"]["rz_balance"] == [24502.771794]


def test_拉取失败时沿用最近一次缓存(monkeypatch):
    margin_service._store.set("rows", {"2026-10-21": {"rz_balance": 12.34}}, ttl_sec=-1)
    monkeypatch.setattr(margin_service, "fetch_market_rows", lambda: {})
    monkeypatch.setattr(
        margin_service,
        "target_trade_dates",
        lambda days: ["2026-10-21"],
    )
    monkeypatch.setattr(margin_service, "today_cn", lambda: date(2026, 10, 22))

    payload = margin_service.get_curve_payload("margin", 30)

    assert payload["metrics"] == [
        {
            "id": "rz_balance",
            "latest": 12.34,
            "latest_date": "2026-10-21",
            "delta": None,
            "delta_pct": None,
        }
    ]


def test_曲线接口把融资融券分组分发给专用服务(monkeypatch):
    calls: list[tuple[str, int, bool]] = []

    def fake_curve(group: str, days: int, *, force: bool = False):
        calls.append((group, days, force))
        return {
            "group": "margin",
            "group_label": "融资融券",
            "scope": "沪深两市",
            "as_of": "2026-10-21",
            "dates": [],
            "metrics": [],
            "series": {},
            "coverage": {"trading_days": 0, "target": 0, "complete": True},
        }

    monkeypatch.setattr(margin_service, "get_curve_payload", fake_curve)
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)

    assert client.get("/api/etf-shares/curve?group=margin&days=30").status_code == 200
    assert calls[-1] == ("margin", 30, False)

    assert client.post("/api/etf-shares/refresh?group=margin&days=30").status_code == 200
    assert calls[-1] == ("margin", 30, True)


def test_数据库重建实例复用并合并修正(monkeypatch):
    from src.modules.market.statistics_store import StatisticsStore

    first = {"2026-10-20": {"rz_balance": 1}, "2026-10-21": {"rz_balance": 2, "rq_balance": 3}}
    monkeypatch.setattr(margin_service, "fetch_market_rows", lambda: first)
    assert margin_service._load_rows(force=True) == first
    monkeypatch.setattr(margin_service, "_store", StatisticsStore("margin", 300))
    monkeypatch.setattr(margin_service, "fetch_market_rows", lambda: {"2026-10-21": {"rz_balance": 4}})
    assert margin_service._load_rows(force=False) == first
    changed = margin_service._load_rows(force=True)
    assert changed == {"2026-10-20": {"rz_balance": 1}, "2026-10-21": {"rz_balance": 4, "rq_balance": 3}}


def test_异常回退不刷新数据时间(monkeypatch, statistics_db):
    from src.platform.persistence.models import StatisticSnapshot

    previous = {"2026-10-21": {"rz_balance": 12.34}}
    margin_service._store.set("rows", previous, ttl_sec=-1)
    with statistics_db() as db:
        before = db.get(StatisticSnapshot, ("margin", "rows")).fetched_at

    def fail():
        raise RuntimeError("controlled upstream failure")

    monkeypatch.setattr(margin_service, "fetch_market_rows", fail)
    assert margin_service._load_rows(force=True) == previous
    with statistics_db() as db:
        assert db.get(StatisticSnapshot, ("margin", "rows")).fetched_at == before
