"""铝锭库存：摘要解析、曲线对齐、持久化与路由分发测试。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.market import aluminum_inventory_service
from src.modules.market.api import etf_shares as etf_shares_api
from src.platform.persistence.database import Base, get_db
from src.platform.persistence.models import AluminumInventoryPoint


@pytest.fixture(autouse=True)
def reset_sync_state():
    aluminum_inventory_service._state["last_sync"] = 0.0
    yield
    aluminum_inventory_service._state["last_sync"] = 0.0


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("社会库存合计由61.6万吨降至58.6万吨", 58.6),
        ("合计由61.6万吨降至58.6万吨", 58.6),
        ("中国主要市场电解铝库存合计为58.6万吨", 58.6),
        ("社会库存总量由 64.2 万吨减少至 61.6 万吨", 61.6),
        ("暂无数据", None),
    ],
)
def test_解析社会库存摘要(text, expected):
    assert aluminum_inventory_service.parse_social_inventory(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("厂内铝锭库存合计由9.1万吨降至7.7万吨", 7.7),
        ("厂内库存合计为7.7万吨", 7.7),
        ("合计由6.0万吨增至6.2万吨", 6.2),
        ("厂内铝锭库存总计5.3万吨", 5.3),
        ("暂无数据", None),
    ],
)
def test_解析厂内库存摘要(text, expected):
    assert aluminum_inventory_service.parse_factory_inventory(text) == expected


def test_曲线对齐库存并计算变化与合计():
    rows = {
        "2026-10-12": {"social_wan": 61.6, "factory_wan": 9.2},
        "2026-10-15": {"social_wan": 58.6, "factory_wan": 9.1},
        "2026-10-19": {"social_wan": 56.6, "factory_wan": None},
        "2026-10-22": {"social_wan": 53.1, "factory_wan": 7.7},
    }

    payload = aluminum_inventory_service.build_payload(rows, 30)

    assert payload["group"] == "aluminumInventory"
    assert payload["scope"] == "Mysteel · 社会库存与厂内库存"
    assert payload["dates"] == ["2026-10-12", "2026-10-15", "2026-10-19", "2026-10-22"]
    assert payload["series"]["social_change"] == [None, -3.0, -2.0, -3.5]
    assert payload["series"]["total"] == [70.8, 67.7, 65.7, 60.8]
    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert metrics["social"]["latest"] == 53.1
    assert metrics["social"]["delta"] == -3.5
    assert metrics["factory"]["latest"] == 7.7
    assert metrics["factory"]["delta"] == -1.4
    assert metrics["total"]["latest"] == 60.8
    assert payload["coverage"]["trading_days"] == 4


def test_缺期过长时不计算每期变化():
    rows = {
        "2026-06-15": {"social_wan": 129.8, "factory_wan": None},
        "2026-07-02": {"social_wan": 112.1, "factory_wan": 6.0},
        "2026-07-06": {"social_wan": 108.8, "factory_wan": None},
    }

    payload = aluminum_inventory_service.build_payload(rows, 30)

    assert payload["series"]["social_change"] == [None, None, -3.3]


def test_按天数窗口筛选库存曲线(monkeypatch):
    monkeypatch.setattr(
        aluminum_inventory_service, "today_cn", lambda: date(2026, 10, 23)
    )
    rows = {
        "2026-10-12": {"social_wan": 61.6, "factory_wan": 9.2},
        "2026-10-19": {"social_wan": 56.6, "factory_wan": None},
        "2026-10-22": {"social_wan": 53.1, "factory_wan": 7.7},
    }

    payload = aluminum_inventory_service.build_payload(rows, 7, full=False)

    assert payload["dates"] == ["2026-10-19", "2026-10-22"]


def test_同步写入数据库且跳过已有期次(db_session, monkeypatch):
    db_session.add_all(
        [
            AluminumInventoryPoint(report_date="2026-10-19", social_wan=56.6),
            AluminumInventoryPoint(report_date="2026-10-22", factory_wan=7.7),
        ]
    )
    db_session.commit()
    captured: dict[str, set[str]] = {}

    def fake_fetch(_cutoff, skip_social=None, skip_factory=None):
        captured["social"] = set(skip_social or set())
        captured["factory"] = set(skip_factory or set())
        return (
            {"2026-10-22": 53.1},
            {"2026-10-19": 5.0},
        )

    monkeypatch.setattr(aluminum_inventory_service, "fetch_inventory", fake_fetch)

    aluminum_inventory_service._sync_rows(db_session)

    assert "2026-10-19" in captured["social"]
    assert "2026-10-22" in captured["factory"]
    records = {
        record.report_date: record
        for record in db_session.query(AluminumInventoryPoint).all()
    }
    assert records["2026-10-19"].social_wan == 56.6
    assert records["2026-10-19"].factory_wan == 5.0
    assert records["2026-10-22"].factory_wan == 7.7
    assert records["2026-10-22"].social_wan == 53.1


def test_曲线接口分发到铝锭库存服务(db_session, monkeypatch):
    db_session.add(
        AluminumInventoryPoint(
            report_date="2026-10-22",
            social_wan=53.1,
            factory_wan=7.7,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        aluminum_inventory_service,
        "_needs_sync",
        lambda _db: False,
    )
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: db_session

    response = TestClient(app).get(
        "/api/etf-shares/curve?group=aluminumInventory&days=30&full=1"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["group"] == "aluminumInventory"
    assert payload["scope"] == "Mysteel · 社会库存与厂内库存"
    assert payload["metrics"][-1]["id"] == "total"
    assert payload["metrics"][-1]["latest"] == 60.8
