"""三峡水库水位：表格解析、每日取数、曲线对齐、持久化与路由分发测试。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.market import three_gorges_service
from src.modules.market.api import etf_shares as etf_shares_api
from src.modules.market.three_gorges_service import (
    DailyPoint,
    Observation,
    parse_station_rows,
    pick_daily_points,
)
from src.platform.persistence.database import Base, get_db
from src.platform.persistence.models import ThreeGorgesWaterPoint


@pytest.fixture(autouse=True)
def reset_sync_state():
    three_gorges_service._state["recent_at"] = -float("inf")
    three_gorges_service._state["history_at"] = -float("inf")
    yield
    three_gorges_service._state["recent_at"] = -float("inf")
    three_gorges_service._state["history_at"] = -float("inf")


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_解析逐小时表格并换算蓄水量():
    page_html = """
    <table>
      <tr><th>时间</th><th>库水位</th><th>蓄水量</th><th>入库</th><th>出库</th></tr>
      <tr><td>2026-10-23 08:00</td><td>174.77</td><td>39068</td><td>10900</td><td>13400</td></tr>
      <tr><td>2026-10-23 09:00</td><td>174.78</td><td>--</td><td></td><td>13500</td></tr>
      <tr><td>不是时间</td><td>174.80</td><td>39070</td><td>10800</td><td>13400</td></tr>
      <tr><td>2026-10-23 10:00</td><td>999</td><td>1</td><td>1</td><td>1</td></tr>
    </table>
    """

    rows = parse_station_rows(page_html)

    assert [row.observed_at for row in rows] == ["2026-10-23 08", "2026-10-23 09"]
    assert rows[0].level_m == 174.77
    assert rows[0].storage_yi_m3 == 390.68
    assert rows[0].inflow_m3s == 10900
    assert rows[0].outflow_m3s == 13400
    assert rows[1].storage_yi_m3 is None
    assert rows[1].inflow_m3s is None
    assert rows[1].outflow_m3s == 13500


def test_每日优先8时观测且缺失时回退最后一条():
    observations = [
        Observation("2026-10-22 08", "2026-10-22", 8, 174.90, 391.00, None, 13000),
        Observation("2026-10-22 14", "2026-10-22", 14, 174.80, 390.80, 11000, 13100),
        Observation("2026-10-23 06", "2026-10-23", 6, 174.78, 390.70, 10800, 13200),
        Observation("2026-10-23 20", "2026-10-23", 20, 174.76, 390.60, 10700, 13300),
    ]

    points = pick_daily_points(observations)

    assert points["2026-10-22"].observed_at == "2026-10-22 08"
    assert points["2026-10-22"].level_m == 174.90
    assert points["2026-10-22"].inflow_m3s == 11000
    assert points["2026-10-23"].observed_at == "2026-10-23 20"
    assert points["2026-10-23"].level_m == 174.76
    assert points["2026-10-23"].storage_yi_m3 == 390.60


def test_曲线对齐并计算指标():
    rows = {
        "2026-10-21": {
            "observed_at": "2026-10-21 08",
            "level_m": 174.86,
            "storage_yi_m3": 391.59,
            "inflow_m3s": 11800,
            "outflow_m3s": 9160,
        },
        "2026-10-22": {
            "observed_at": "2026-10-22 08",
            "level_m": 174.86,
            "storage_yi_m3": 391.59,
            "inflow_m3s": 11800,
            "outflow_m3s": 9160,
        },
        "2026-10-23": {
            "observed_at": "2026-10-23 08",
            "level_m": 174.77,
            "storage_yi_m3": 390.68,
            "inflow_m3s": 10900,
            "outflow_m3s": 13400,
        },
    }

    payload = three_gorges_service.build_payload(
        rows, 3, today=date(2026, 10, 23)
    )

    assert payload["group"] == "threeGorges"
    assert payload["scope"] == "湖北水文 · 三峡站 60106980"
    assert payload["as_of"] == "2026-10-23 08"
    assert payload["dates"] == ["2026-10-21", "2026-10-22", "2026-10-23"]
    assert payload["series"]["level"] == [174.86, 174.86, 174.77]
    assert set(payload["series"]) == {"level", "inflow", "outflow"}
    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert set(metrics) == {"level", "inflow", "outflow"}
    assert metrics["level"]["latest"] == 174.77
    assert metrics["level"]["delta"] == -0.09
    assert metrics["inflow"]["delta"] == -900
    assert metrics["outflow"]["delta"] == 4240
    assert payload["coverage"] == {
        "trading_days": 3,
        "target": 3,
        "complete": True,
    }


def test_缺少前一日时不计算变化():
    rows = {
        "2026-10-19": {
            "observed_at": "2026-10-19 08",
            "level_m": 174.90,
            "storage_yi_m3": 391.70,
            "inflow_m3s": 11000,
            "outflow_m3s": 9000,
        },
        "2026-10-21": {
            "observed_at": "2026-10-21 08",
            "level_m": 174.86,
            "storage_yi_m3": 391.59,
            "inflow_m3s": 11800,
            "outflow_m3s": 9160,
        },
    }

    payload = three_gorges_service.build_payload(
        rows, 3, today=date(2026, 10, 21)
    )

    assert payload["dates"] == ["2026-10-19", "2026-10-20", "2026-10-21"]
    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert metrics["level"]["delta"] is None


def test_同步写入数据库并保留已有非空字段(db_session, monkeypatch):
    db_session.add(
        ThreeGorgesWaterPoint(
            report_date="2026-10-22",
            observed_at="2026-10-22 08",
            level_m=174.80,
            storage_yi_m3=390.70,
            inflow_m3s=10900,
            outflow_m3s=9100,
        )
    )
    db_session.commit()

    def fake_fetch(_start, _end, *, max_windows):
        assert max_windows >= 1
        return {
            "2026-10-22": DailyPoint(
                day="2026-10-22",
                observed_at="2026-10-22 08",
                level_m=174.77,
                storage_yi_m3=None,
                inflow_m3s=None,
                outflow_m3s=13400,
            ),
            "2026-10-23": DailyPoint(
                day="2026-10-23",
                observed_at="2026-10-23 08",
                level_m=174.69,
                storage_yi_m3=390.20,
                inflow_m3s=10800,
                outflow_m3s=13500,
            ),
        }

    monkeypatch.setattr(three_gorges_service, "_fetch_range", fake_fetch)

    three_gorges_service._sync_rows(
        db_session, date(2026, 10, 22), date(2026, 10, 23), force=True
    )

    records = {
        record.report_date: record
        for record in db_session.query(ThreeGorgesWaterPoint).all()
    }
    assert records["2026-10-22"].level_m == 174.77
    assert records["2026-10-22"].storage_yi_m3 == 390.70
    assert records["2026-10-22"].inflow_m3s == 10900
    assert records["2026-10-22"].outflow_m3s == 13400
    assert records["2026-10-23"].level_m == 174.69
    assert records["2026-10-23"].storage_yi_m3 == 390.20


def test_新进程启动后立即补齐近期与历史(db_session, monkeypatch):
    calls: list[tuple[date, date, int]] = []

    def fake_fetch(start, end, *, max_windows):
        calls.append((start, end, max_windows))
        return {}

    monkeypatch.setattr(three_gorges_service, "_fetch_range", fake_fetch)
    monkeypatch.setattr(
        three_gorges_service.time, "monotonic", lambda: 1000.0
    )

    three_gorges_service._sync_rows(
        db_session, date(2026, 6, 26), date(2026, 10, 23), force=False
    )

    assert calls == [
        (date(2026, 9, 24), date(2026, 10, 23), 1),
        (date(2026, 6, 26), date(2026, 9, 23), 3),
    ]


def test_空页只短暂退避而非按正常周期节流(db_session, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(three_gorges_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        three_gorges_service, "_fetch_range", lambda *_args, **_kwargs: {}
    )

    three_gorges_service._sync_rows(
        db_session, date(2026, 7, 26), date(2026, 10, 23), force=False
    )

    assert three_gorges_service._needs_sync(db_session) is False
    clock[0] += three_gorges_service._EMPTY_RETRY_S + 1
    assert three_gorges_service._needs_sync(db_session) is True


def test_曲线接口分发到三峡水库服务(db_session, monkeypatch):
    db_session.add_all(
        [
            ThreeGorgesWaterPoint(
                report_date="2026-10-22",
                observed_at="2026-10-22 08",
                level_m=174.86,
                storage_yi_m3=391.59,
                inflow_m3s=11800,
                outflow_m3s=9160,
            ),
            ThreeGorgesWaterPoint(
                report_date="2026-10-23",
                observed_at="2026-10-23 08",
                level_m=174.77,
                storage_yi_m3=390.68,
                inflow_m3s=10900,
                outflow_m3s=13400,
            ),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(
        three_gorges_service, "today_cn", lambda: date(2026, 10, 23)
    )
    monkeypatch.setattr(three_gorges_service, "_needs_sync", lambda _db: False)
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: db_session

    response = TestClient(app).get(
        "/api/etf-shares/curve?group=threeGorges&days=30"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["group"] == "threeGorges"
    assert payload["scope"] == "湖北水文 · 三峡站 60106980"
    assert payload["as_of"] == "2026-10-23 08"
    assert [metric["id"] for metric in payload["metrics"]] == [
        "level",
        "inflow",
        "outflow",
    ]
    assert set(payload["series"]) == {"level", "inflow", "outflow"}
    assert payload["coverage"]["complete"] is True
