"""电解铝/氧化铝：解析、对齐、持久化与路由分发测试。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.market import aluminum_service
from src.modules.market.api import etf_shares as etf_shares_api
from src.platform.persistence.database import Base, get_db
from src.platform.persistence.models import AluminumPricePoint, AluminumSpotPoint

_CNAL_HTML = """
<table>
<tr><td>铝</td><td>23680-23720</td><td>23700</td><td>90</td> <td>26-10-22</td></tr>
<tr><td>铝</td><td>23590-23630</td><td>23610</td><td>90</td> <td>26-10-21</td></tr>
<tr><td>铝</td><td>23500-23540</td><td>23520</td><td>-130</td> <td>26-10-20</td></tr>
<tr><td>区间均价</td><td>23590-23630</td><td>23610</td><td></td> <td></td></tr>
</table>
"""

_PPI_HTML = """
<p>中国铝业股份有限公司2026年10月21日氧化铝现货价格维稳，各地区具体如下：</p>
<p>山东地区氧化铝现货对外报价2690元/吨，较上一交易日报价持平；</p>
<p>山西地区氧化铝现货对外报价2735元/吨，较上一交易日报价持平。</p>
"""


@pytest.fixture(autouse=True)
def reset_sync_state():
    aluminum_service._state["last_sync"] = 0.0
    aluminum_service._state["spot_sync_at"] = -float("inf")
    yield
    aluminum_service._state["last_sync"] = 0.0
    aluminum_service._state["spot_sync_at"] = -float("inf")


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


def test_解析世铝网A00报价():
    rows = aluminum_service.parse_cnal_html(_CNAL_HTML, today=date(2026, 10, 23))

    assert list(rows) == ["2026-10-20", "2026-10-21", "2026-10-22"]
    latest = rows["2026-10-22"]
    assert (latest.low, latest.high, latest.avg, latest.change) == (
        23680,
        23720,
        23700,
        90,
    )


def test_解析生意社山西氧化铝报价():
    assert aluminum_service.parse_ppi_alumina(_PPI_HTML) == 2735
    assert aluminum_service.parse_ppi_alumina("<p>暂无报价</p>") is None


def test_解析生意社现货铝日报价():
    html = """
    <tr>
      <td><a href="https://www.100ppi.com/sf/827.html" style="color:#21469f;"
             target=_blank>铝</a></td>
      <td>20693.33&nbsp;</td>
    </tr>
    """

    assert aluminum_service.parse_ppi_spot(html) == 20693.33
    assert aluminum_service.parse_ppi_spot("<p>暂无报价</p>") is None


def test_曲线对齐铝价与氧化铝并计算价差():
    rows = {
        "2026-10-20": {
            "a00_low": 23500,
            "a00_high": 23540,
            "a00_avg": 23520,
            "alumina_avg": 2735,
        },
        "2026-10-21": {
            "a00_low": 23590,
            "a00_high": 23630,
            "a00_avg": 23610,
            "alumina_avg": None,
        },
        "2026-10-22": {
            "a00_low": 23680,
            "a00_high": 23720,
            "a00_avg": 23700,
            "alumina_avg": 2730,
        },
    }

    payload = aluminum_service.build_payload(rows, 30)

    assert payload["group"] == "aluminum"
    assert payload["dates"] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert payload["series"]["alumina_avg"] == [2735, 2735, 2730]
    assert payload["series"]["spread"] == [20785, 20875, 20970]
    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert metrics["a00_avg"]["latest"] == 23700
    assert metrics["a00_avg"]["delta"] == 90
    assert metrics["alumina_avg"]["latest"] == 2730
    assert metrics["spread"]["latest"] == 20970
    assert payload["coverage"]["trading_days"] == 3


def test_氧化铝报价在窗口起点沿用前值():
    rows = {
        "2026-10-19": {"a00_avg": 23440, "alumina_avg": 2765},
        "2026-10-20": {"a00_avg": 23520, "alumina_avg": None},
        "2026-10-21": {"a00_avg": 23610, "alumina_avg": None},
        "2026-10-22": {"a00_avg": 23700, "alumina_avg": 2735},
    }

    payload = aluminum_service.build_payload(rows, 3)

    assert payload["dates"] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert payload["series"]["alumina_avg"] == [2765, 2765, 2735]
    assert payload["series"]["spread"][0] == 20755


def test_叠加曲线对齐上年同期现货铝():
    rows = {
        "2026-10-20": {
            "a00_low": 23500,
            "a00_high": 23540,
            "a00_avg": 23520,
            "alumina_avg": 2735,
        },
        "2026-10-21": {
            "a00_low": 23590,
            "a00_high": 23630,
            "a00_avg": 23610,
            "alumina_avg": 2735,
        },
        "2026-10-22": {
            "a00_low": 23680,
            "a00_high": 23720,
            "a00_avg": 23700,
            "alumina_avg": 2730,
        },
    }

    payload = aluminum_service.build_payload(
        rows,
        3,
        {"2025-10-20": 20500.0, "2025-10-22": 20600.0},
    )

    assert payload["overlay"]["dates"] == [
        "2026-10-20",
        "2025-10-20",
        "2026-10-21",
        "2025-10-21",
        "2026-10-22",
        "2025-10-22",
    ]
    assert payload["overlay"]["values"] == [
        23520,
        20500.0,
        23610,
        None,
        23700,
        20600.0,
    ]


def test_现货叠加只补齐缺失的上年日期(db_session, monkeypatch):
    db_session.add(AluminumSpotPoint(trade_date="2025-10-20", spot_price=20500.0))
    db_session.commit()
    fetched: list[str] = []

    def fake_fetch(day: str) -> float:
        fetched.append(day)
        return 20600.0

    monkeypatch.setattr(aluminum_service, "_fetch_ppi_spot", fake_fetch)

    aluminum_service._sync_spot_rows(
        db_session,
        ["2026-10-20", "2026-10-21", "2026-10-22"],
        force=False,
    )

    assert set(fetched) == {"2025-10-21", "2025-10-22"}
    rows = {
        str(record.trade_date): record.spot_price
        for record in db_session.query(AluminumSpotPoint).all()
    }
    assert rows == {
        "2025-10-20": 20500.0,
        "2025-10-21": 20600.0,
        "2025-10-22": 20600.0,
    }


def test_同步写入数据库且不覆盖已有氧化铝报价(db_session, monkeypatch):
    db_session.add(
        AluminumPricePoint(
            trade_date="2026-10-21",
            a00_avg=23610,
            alumina_avg=2735,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        aluminum_service,
        "fetch_cnal_a00",
        lambda _start, _end: {
            "2026-10-21": aluminum_service.A00Quote(23590, 23630, 23610, 90),
            "2026-10-22": aluminum_service.A00Quote(23680, 23720, 23700, 90),
        },
    )
    captured: list[set[str]] = []

    def fake_alumina(_cutoff, skip_days=None):
        captured.append(set(skip_days or set()))
        return {"2026-10-22": 2730}

    monkeypatch.setattr(aluminum_service, "fetch_ppi_alumina", fake_alumina)
    monkeypatch.setattr(
        aluminum_service, "_sync_spot_rows", lambda *_args, **_kwargs: None
    )

    payload = aluminum_service.get_curve_payload(
        db_session, "aluminum", 30, force=True
    )

    assert "2026-10-21" in captured[0]
    records = {
        record.trade_date: record
        for record in db_session.query(AluminumPricePoint).all()
    }
    assert records["2026-10-21"].a00_low == 23590
    assert records["2026-10-22"].alumina_avg == 2730
    assert payload["series"]["spread"][-1] == 20970


def test_曲线接口分发到电解铝服务(db_session, monkeypatch):
    db_session.add(
        AluminumPricePoint(
            trade_date="2026-10-22",
            a00_low=23680,
            a00_high=23720,
            a00_avg=23700,
            alumina_avg=2735,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        aluminum_service,
        "_needs_sync",
        lambda _db, _days: False,
    )
    monkeypatch.setattr(
        aluminum_service, "_sync_spot_rows", lambda *_args, **_kwargs: None
    )
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: db_session

    response = TestClient(app).get("/api/etf-shares/curve?group=aluminum&days=30")

    assert response.status_code == 200
    payload = response.json()
    assert payload["group"] == "aluminum"
    assert payload["scope"] == "A00铝 · 氧化铝(山西)"
    assert payload["metrics"][-1]["id"] == "spread"
