"""动力煤月度长协价：CCTD 响应解析、种子数据、同步回填、窗口与路由分发测试。"""

from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.market import coal_ltc_service
from src.modules.market.api import etf_shares as etf_shares_api
from src.modules.market.etf_share_service import catalog_payload
from src.platform.persistence.database import Base, get_db
from src.platform.persistence.models import CoalLtcPoint  # noqa: F401 - 注册表结构

_MONTH_RE = re.compile(r"^20\d{2}-(0[1-9]|1[0-2])$")

_ROWS = [
    {
        "month": "2025-11",
        "price_5500": 684.0,
        "price_5000": 622.0,
        "price_4500": 560.0,
    },
    {
        "month": "2025-12",
        "price_5500": 694.0,
        "price_5000": 631.0,
        "price_4500": 568.0,
    },
    {
        "month": "2026-01",
        "price_5500": 684.0,
        "price_5000": 622.0,
        "price_4500": 560.0,
    },
    {
        "month": "2026-02",
        "price_5500": 680.0,
        "price_5000": 618.0,
        "price_4500": 556.0,
    },
]


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


@pytest.fixture
def sample_seed(monkeypatch):
    monkeypatch.setattr(
        coal_ltc_service,
        "load_seed_rows",
        lambda path=None: [dict(row) for row in _ROWS],
    )


@pytest.fixture
def api_client(db_session, sample_seed):
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_解析CCTD长协响应():
    payload = {
        "recordsTotal": 3,
        "data": [
            ["2026-11", "727", "10", "661", "9", "595", "8"],
            ["2026-10", "717", "13", "652", "12", "587", "11"],
            ["2026-09", "704", "3", "640", "3", "", ""],
            ["坏数据", "", "", "", "", "", ""],
            ["1999-13", "1", "0", "1", "0", "1", "0"],
        ],
    }
    assert coal_ltc_service.parse_data_rows(payload) == [
        {
            "month": "2026-11",
            "price_5500": 727.0,
            "price_5000": 661.0,
            "price_4500": 595.0,
        },
        {
            "month": "2026-10",
            "price_5500": 717.0,
            "price_5000": 652.0,
            "price_4500": 587.0,
        },
        {
            "month": "2026-09",
            "price_5500": 704.0,
            "price_5000": 640.0,
            "price_4500": None,
        },
    ]
    assert coal_ltc_service.parse_data_rows(None) == []
    assert coal_ltc_service.parse_data_rows({"data": []}) == []


def test_请求地址使用GBK编码的数据集名():
    url = coal_ltc_service._data_url()
    assert url.startswith("https://www.cctd.com.cn/datasql.php?data=")
    assert "CCTD%C7%D8%BB%CA%B5%BA%B3%A4%D0%AD%BC%DB%B8%F1" in url
    assert "%27%25Y-%25m-%25d%27" in url


def test_曲线含三档热值与环比(db_session, sample_seed, monkeypatch):
    monkeypatch.setattr(coal_ltc_service, "today_cn", lambda: date(2026, 3, 5))
    payload = coal_ltc_service.get_curve_payload(db_session, "coalLtc", 90, full=True)

    assert payload["group"] == "coalLtc"
    assert payload["group_label"] == "动力煤月度长协价"
    assert payload["dates"] == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert payload["as_of"] == "2026-02"
    assert payload["series"]["ltc_5500"] == [684.0, 694.0, 684.0, 680.0]
    assert payload["series"]["ltc_4500"] == [560.0, 568.0, 560.0, 556.0]

    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert metrics["ltc_5500"]["latest"] == 680.0
    assert metrics["ltc_5500"]["delta"] == -4.0
    assert metrics["ltc_5500"]["delta_pct"] == pytest.approx(-0.5848, abs=1e-4)
    assert metrics["ltc_5000"]["latest"] == 618.0
    assert payload["source"]["name"] == "CCTD 中国煤炭市场网"
    assert payload["storage"]["table"] == "coal_ltc_points"


def test_窗口按自然日裁剪(db_session, sample_seed, monkeypatch):
    monkeypatch.setattr(coal_ltc_service, "today_cn", lambda: date(2026, 2, 10))
    payload = coal_ltc_service.get_curve_payload(db_session, "coalLtc", 90)
    assert payload["dates"] == ["2025-11", "2025-12", "2026-01", "2026-02"]

    payload = coal_ltc_service.get_curve_payload(db_session, "coalLtc", 30)
    assert payload["dates"] == ["2026-01", "2026-02"]


def test_同步新增与修订月份(db_session, sample_seed, monkeypatch):
    assert coal_ltc_service.get_curve_payload(db_session, "coalLtc", 90, full=True)
    monkeypatch.setattr(
        coal_ltc_service,
        "fetch_cctd_rows",
        lambda: [
            {"month": "2026-02", "price_5500": 681.0, "price_5000": 618.0, "price_4500": 556.0},
            {"month": "2026-03", "price_5500": 682.0, "price_5000": 620.0, "price_4500": 558.0},
        ],
    )
    assert coal_ltc_service.sync_from_cctd(db_session) == 2

    rows = {row["month"]: row for row in coal_ltc_service.load_points(db_session)}
    assert rows["2026-02"]["price_5500"] == 681.0
    assert rows["2026-03"]["price_5500"] == 682.0


def test_同步失败时保留本地数据(db_session, sample_seed, monkeypatch):
    assert coal_ltc_service.get_curve_payload(db_session, "coalLtc", 90, full=True)

    def broken() -> list[dict]:
        raise RuntimeError("接口不可用")

    monkeypatch.setattr(coal_ltc_service, "fetch_cctd_rows", broken)
    assert coal_ltc_service.sync_from_cctd(db_session) == 0
    assert len(coal_ltc_service.load_points(db_session)) == 4


def test_内置种子为连续月度长协价():
    rows = coal_ltc_service.load_seed_rows()
    months = [row["month"] for row in rows]
    assert len(rows) >= 100
    assert months == sorted(months)
    assert len(set(months)) == len(months)
    assert all(_MONTH_RE.match(month) for month in months)
    assert months[-1][:4] == str(date.today().year)
    for row in rows:
        assert row["price_5500"] > (row["price_5000"] or 0) > (row["price_4500"] or 0)


def test_目录包含动力煤长协价分组():
    group = {
        item["id"]: item for item in catalog_payload()["groups"]
    }["coalLtc"]
    assert group["available"] is True
    assert group["category"] == "commodity"
    assert group["label"] == "动力煤月度长协价"


def test_路由分发到动力煤长协价(api_client):
    response = api_client.get(
        "/api/etf-shares/curve?group=coalLtc&days=90&full=1"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["group"] == "coalLtc"
    assert payload["dates"] == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert payload["series"]["ltc_5500"] == [684.0, 694.0, 684.0, 680.0]
