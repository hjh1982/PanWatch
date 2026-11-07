"""融资融券市场情绪：全市场历史解析、缓存与路由分发测试。"""

from __future__ import annotations

from datetime import date, timedelta

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
    monkeypatch.setattr(margin_service, "fetch_history", lambda **_: ({}, False))
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
    monkeypatch.setattr(margin_service, "fetch_history", lambda **_: (first, True))
    assert margin_service._load_rows(force=True) == first
    monkeypatch.setattr(margin_service, "_store", StatisticsStore("margin", 300))
    monkeypatch.setattr(
        margin_service,
        "fetch_history",
        lambda **_: ({"2026-10-21": {"rz_balance": 4}}, True),
    )
    assert margin_service._load_rows(force=False) == first
    changed = margin_service._load_rows(force=True)
    assert changed == {"2026-10-20": {"rz_balance": 1}, "2026-10-21": {"rz_balance": 4, "rq_balance": 3}}


def test_异常回退不刷新数据时间(monkeypatch, statistics_db):
    from src.platform.persistence.models import StatisticSnapshot

    previous = {"2026-10-21": {"rz_balance": 12.34}}
    margin_service._store.set("rows", previous, ttl_sec=-1)
    with statistics_db() as db:
        before = db.get(StatisticSnapshot, ("margin", "rows")).fetched_at

    def fail(**_):
        raise RuntimeError("controlled upstream failure")

    monkeypatch.setattr(margin_service, "fetch_history", fail)
    assert margin_service._load_rows(force=True) == previous
    with statistics_db() as db:
        assert db.get(StatisticSnapshot, ("margin", "rows")).fetched_at == before


def _paged_payload(page_number: int, pages: int) -> dict:
    return {
        "result": {
            "pages": pages,
            "data": [
                {
                    "DIM_DATE": f"2026-01-{page_number:02d} 00:00:00",
                    "RZRQYE": (10_000 + page_number) * 1_000_000_000,
                }
            ],
        }
    }


def test_首次全量分页回填后续请求只取最新一页(monkeypatch):
    seen: list[int] = []

    def fake_market_get(url, **kwargs):
        page_number = int(kwargs["params"]["pageNumber"])
        seen.append(page_number)
        return _paged_payload(page_number, 3)

    monkeypatch.setattr(margin_service, "market_get", fake_market_get)

    rows, complete = margin_service.fetch_history(full=True)
    assert complete is True
    assert seen == [1, 2, 3]
    assert sorted(rows) == ["2026-01-01", "2026-01-02", "2026-01-03"]

    seen.clear()
    recent, incremental = margin_service.fetch_history(full=False)
    assert incremental is True and seen == [1] and recent

    margin_service._load_rows(force=True)
    assert margin_service._store.get(margin_service._BACKFILL_KEY, allow_stale=True)
    seen.clear()
    margin_service._load_rows(force=True)
    assert seen == [1]


def test_回填中途断页时不写完成标记(monkeypatch):
    def fake_market_get(url, **kwargs):
        page_number = int(kwargs["params"]["pageNumber"])
        if page_number == 2:
            return {"result": {"pages": 3, "data": None}}
        return _paged_payload(page_number, 3)

    monkeypatch.setattr(margin_service, "market_get", fake_market_get)
    rows, complete = margin_service.fetch_history(full=True)

    assert complete is False and sorted(rows) == ["2026-01-01"]
    margin_service._load_rows(force=True)
    assert margin_service._store.get(margin_service._BACKFILL_KEY, allow_stale=True) is None


def _linear_rows(count: int, rise_until: int, peak: float, slope: float) -> dict[str, dict[str, float]]:
    start = date(2020, 1, 1)
    rows: dict[str, dict[str, float]] = {}
    for index in range(count):
        value = peak - (index - rise_until) * slope if index > rise_until else 100.0 + index
        rows[(start + timedelta(days=index)).isoformat()] = {"total_balance": value}
    return rows


def test_滚动一年高点回撤与预热期(monkeypatch):
    monkeypatch.setattr(margin_service, "_DRAWDOWN_WINDOW", 50)
    monkeypatch.setattr(margin_service, "_SLOW_DAYS", 10)
    monkeypatch.setattr(margin_service, "_FAST_DAYS", 2)

    rows = _linear_rows(80, rise_until=50, peak=150.0, slope=1.0)
    block = margin_service.build_drawdown(rows)

    assert block["window"] == 50 and block["warmup_days"] == 49
    assert len(block["dates"]) == 80 == block["history_days"]
    assert set(block["drawdown_pct"][:49]) == {None}
    assert block["drawdown_pct"][49] == pytest.approx(0.0)
    assert block["drawdown_pct"][79] == pytest.approx(-19.3333, abs=1e-4)
    assert block["metric"] == "total_balance"


def test_预热天数按展示窗口折算(monkeypatch):
    monkeypatch.setattr(margin_service, "_DRAWDOWN_WINDOW", 50)
    monkeypatch.setattr(margin_service, "_MAX_POINTS", 20)

    block = margin_service.build_drawdown(_linear_rows(80, rise_until=50, peak=150.0, slope=1.0))

    assert len(block["dates"]) == 20
    assert block["warmup_days"] == 0

    monkeypatch.setattr(margin_service, "_MAX_POINTS", 60)
    partial = margin_service.build_drawdown(_linear_rows(80, rise_until=50, peak=150.0, slope=1.0))
    assert len(partial["dates"]) == 60
    assert partial["warmup_days"] == 29


def test_回撤速度距高点与当前状态(monkeypatch):
    monkeypatch.setattr(margin_service, "_DRAWDOWN_WINDOW", 50)
    monkeypatch.setattr(margin_service, "_SLOW_DAYS", 10)
    monkeypatch.setattr(margin_service, "_FAST_DAYS", 2)

    rows = _linear_rows(80, rise_until=50, peak=150.0, slope=1.0)
    latest = margin_service.build_drawdown(rows)["latest"]

    assert latest["high_value"] == pytest.approx(150.0)
    assert latest["value"] == pytest.approx(121.0)
    assert latest["days_since_high"] == 29
    assert latest["max_drawdown_pct"] == pytest.approx(-19.3333, abs=1e-4)
    assert latest["rebound_needed_pct"] == pytest.approx(23.9669, abs=1e-4)
    assert latest["velocity_slow"] == pytest.approx(-0.666667, abs=1e-6)
    assert latest["velocity_fast"] == pytest.approx(-0.666667, abs=1e-6)
    assert latest["state"] == "decelerating"


def test_回撤与高点齐平时状态为接近高点(monkeypatch):
    monkeypatch.setattr(margin_service, "_DRAWDOWN_WINDOW", 30)
    monkeypatch.setattr(margin_service, "_SLOW_DAYS", 5)
    monkeypatch.setattr(margin_service, "_FAST_DAYS", 2)

    rows = _linear_rows(60, rise_until=59, peak=100.0, slope=0.0)
    latest = margin_service.build_drawdown(rows)["latest"]

    assert latest["drawdown_pct"] == pytest.approx(0.0)
    assert latest["state"] == "near_high"


def test_历史不足滚动窗口时不编造回撤(monkeypatch):
    monkeypatch.setattr(margin_service, "_DRAWDOWN_WINDOW", 250)

    block = margin_service.build_drawdown(_linear_rows(120, 119, 100.0, 0.0))

    assert block["latest"] is None
    assert set(block["drawdown_pct"]) == {None}
    assert set(block["velocity_slow"]) == {None}
    assert block["warmup_days"] == 120


def test_回撤块随曲线接口返回(monkeypatch):
    monkeypatch.setattr(margin_service, "_DRAWDOWN_WINDOW", 3)
    monkeypatch.setattr(margin_service, "_SLOW_DAYS", 2)
    monkeypatch.setattr(margin_service, "_FAST_DAYS", 1)
    monkeypatch.setattr(margin_service, "target_trade_dates", lambda days: ["2026-10-21"])
    monkeypatch.setattr(margin_service, "today_cn", lambda: date(2026, 10, 22))
    monkeypatch.setattr(
        margin_service,
        "fetch_history",
        lambda **_: (
            {
                "2026-10-17": {"total_balance": 100.0},
                "2026-10-18": {"total_balance": 100.0},
                "2026-10-19": {"total_balance": 90.0},
                "2026-10-20": {"total_balance": 80.0},
                "2026-10-21": {"total_balance": 60.0},
            },
            True,
        ),
    )

    drawdown = margin_service.get_curve_payload("margin", 30)["drawdown"]

    assert drawdown["dates"] == [
        "2026-10-17",
        "2026-10-18",
        "2026-10-19",
        "2026-10-20",
        "2026-10-21",
    ]
    assert drawdown["drawdown_pct"][:2] == [None, None]
    assert drawdown["drawdown_pct"][2:] == [
        pytest.approx(-10.0),
        pytest.approx(-20.0),
        pytest.approx(-33.3333, abs=1e-4),
    ]
    latest = drawdown["latest"]
    assert latest["high_date"] == "2026-10-19"
    assert latest["days_since_high"] == 2
    assert latest["velocity_slow"] == pytest.approx(-11.6667, abs=1e-4)
    assert latest["velocity_fast"] == pytest.approx(-13.3333, abs=1e-4)
    assert latest["rebound_needed_pct"] == pytest.approx(50.0, abs=1e-4)
    assert latest["state"] == "accelerating"
