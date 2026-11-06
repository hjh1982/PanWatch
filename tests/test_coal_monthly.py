"""煤炭月度：统计局表格解析、吨煤利润折算、窗口、持久化与路由分发测试。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.market import coal_monthly_service
from src.modules.market.api import etf_shares as etf_shares_api
from src.modules.market.etf_share_service import catalog_payload
from src.platform.persistence.database import Base, get_db
from src.platform.persistence.models import CoalMonthlyPoint  # noqa: F401 - 注册表结构

_SAMPLE_ROWS = [
    {
        "month": "2025-02",
        "output_10k_t": 76533,
        "output_cum_10k_t": 76533,
        "profit_cum_yi": 506.6,
        "power_profit_cum_yi": 1123.2,
    },
    {
        "month": "2025-03",
        "output_10k_t": 44058,
        "output_cum_10k_t": 120260,
        "profit_cum_yi": 803.8,
        "power_profit_cum_yi": 1777.4,
    },
    {
        "month": "2025-04",
        "output_10k_t": 38931,
        "output_cum_10k_t": 158474,
        "profit_cum_yi": 1026.8,
        "power_profit_cum_yi": 2359.4,
    },
]

_OUTPUT_HTML = """
<table>
  <tr><td>原煤（万吨）</td><td>36182</td><td>-7.7</td><td>306443</td><td>-3.3</td></tr>
</table>
"""

_OUTPUT_COMBINED_HTML = """
<table>
  <tr><td>原煤（万吨）</td><td>76289</td><td>-0.3</td></tr>
</table>
"""

_PROFIT_HTML = """
<table>
  <tr>
    <td>煤炭开采和洗选业</td><td>18267.0</td><td>10.4</td>
    <td>12573.2</td><td>4.1</td><td>2943.8</td><td>51.6</td>
  </tr>
  <tr>
    <td>电力、热力生产和供应业</td><td>39498.4</td><td>4.7</td>
    <td>35335.1</td><td>3.4</td><td>4462.5</td><td>19.8</td>
  </tr>
</table>
"""


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
        coal_monthly_service,
        "load_seed_rows",
        lambda path=None: [dict(row) for row in _SAMPLE_ROWS],
    )


@pytest.fixture
def api_client(db_session, sample_seed):
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_解析统计局产量与利润表格():
    assert coal_monthly_service.parse_coal_output_table(_OUTPUT_HTML) == (
        36182,
        306443,
    )
    assert coal_monthly_service.parse_coal_output_table(_OUTPUT_COMBINED_HTML) == (
        76289,
        76289,
    )
    assert coal_monthly_service.parse_coal_profit_table(_PROFIT_HTML) == 2943.8
    assert coal_monthly_service.parse_industry_profit_table(_PROFIT_HTML) == {
        "profit_cum_yi": 2943.8,
        "power_profit_cum_yi": 4462.5,
    }


def test_解析月度文章标题与合并月份():
    output = coal_monthly_service.parse_article(
        "2026年8月份规模以上工业增加值增长5.2%", _OUTPUT_HTML
    )
    assert output == {
        "month": "2026-08",
        "output_10k_t": 36182,
        "output_cum_10k_t": 306443,
    }

    combined = coal_monthly_service.parse_article(
        "2026年1—2月份规模以上工业增加值增长6.3%", _OUTPUT_COMBINED_HTML
    )
    assert combined == {
        "month": "2026-02",
        "output_10k_t": 76289,
        "output_cum_10k_t": 76289,
    }

    annual = coal_monthly_service.parse_article(
        "2025年全国规模以上工业企业利润增长0.6%", _PROFIT_HTML
    )
    assert annual == {
        "month": "2025-12",
        "profit_cum_yi": 2943.8,
        "power_profit_cum_yi": 4462.5,
    }


def test_吨煤利润由累计利润差分得到(db_session, sample_seed, monkeypatch):
    monkeypatch.setattr(
        coal_monthly_service, "today_cn", lambda: date(2025, 5, 20)
    )
    payload = coal_monthly_service.get_curve_payload(
        db_session,
        coal_monthly_service.COAL_MONTHLY_GROUP_ID,
        90,
        full=True,
    )

    assert payload["group"] == "coalMonthly"
    assert payload["group_label"] == "煤炭产量·吨煤利润"
    assert payload["dates"] == ["2025-02", "2025-03", "2025-04"]
    assert payload["as_of"] == "2025-04"
    assert payload["series"]["coal_output"] == [76533, 44058, 38931]
    assert payload["series"]["coal_profit"] == [506.6, 297.2, 223.0]
    assert payload["series"]["coal_profit_per_ton"] == [66.19, 67.46, 57.28]
    assert payload["series"]["coal_profit_per_ton_cum"] == [66.19, 66.84, 64.79]
    assert payload["series"]["power_profit"] == [1123.2, 654.2, 582.0]
    assert payload["series"]["power_profit_cum"] == [1123.2, 1777.4, 2359.4]
    assert payload["series"]["industry_profit_total"] == [1629.8, 951.4, 805.0]

    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert metrics["coal_output"]["latest"] == 38931
    assert metrics["coal_output"]["delta"] == -5127
    assert metrics["coal_profit_per_ton"]["latest"] == 57.28
    assert metrics["coal_profit"]["latest"] == 223.0
    assert payload["storage"]["table"] == "coal_monthly_points"
    assert payload["storage"]["seed"].endswith("coal_monthly.csv")
    assert payload["source"]["name"] == "国家统计局"


def test_窗口按自然日截取但保留环比上下文(db_session, sample_seed, monkeypatch):
    monkeypatch.setattr(
        coal_monthly_service, "today_cn", lambda: date(2025, 5, 20)
    )
    payload = coal_monthly_service.get_curve_payload(
        db_session,
        coal_monthly_service.COAL_MONTHLY_GROUP_ID,
        40,
    )

    assert payload["dates"] == ["2025-04"]
    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert metrics["coal_profit_per_ton"]["latest"] == 57.28
    assert metrics["coal_profit_per_ton"]["delta"] is None


def test_首次读取写入种子且重复读取不重复(db_session, sample_seed):
    coal_monthly_service.get_curve_payload(
        db_session,
        coal_monthly_service.COAL_MONTHLY_GROUP_ID,
        90,
        full=True,
    )
    assert db_session.query(CoalMonthlyPoint).count() == 3

    coal_monthly_service.get_curve_payload(
        db_session,
        coal_monthly_service.COAL_MONTHLY_GROUP_ID,
        90,
        full=True,
    )
    assert db_session.query(CoalMonthlyPoint).count() == 3


def test_同步会新增最新月份并计算吨煤利润(db_session, sample_seed, monkeypatch):
    monkeypatch.setattr(
        coal_monthly_service, "today_cn", lambda: date(2025, 6, 20)
    )
    coal_monthly_service.get_curve_payload(
        db_session,
        coal_monthly_service.COAL_MONTHLY_GROUP_ID,
        90,
        full=True,
    )
    monkeypatch.setattr(
        coal_monthly_service,
        "fetch_latest_articles",
        lambda: [
            {
                "month": "2025-05",
                "output_10k_t": 40328,
                "output_cum_10k_t": 198537,
                "profit_cum_yi": 1263.8,
                "power_profit_cum_yi": 2925.0,
            }
        ],
    )

    assert coal_monthly_service.sync_from_nbs(db_session) == 1

    payload = coal_monthly_service.get_curve_payload(
        db_session,
        coal_monthly_service.COAL_MONTHLY_GROUP_ID,
        90,
        full=True,
    )
    assert payload["dates"][-1] == "2025-05"
    assert payload["series"]["coal_profit"][-1] == 237.0
    assert payload["series"]["coal_profit_per_ton"][-1] == 58.77
    assert payload["series"]["power_profit"][-1] == 565.6
    assert payload["series"]["industry_profit_total"][-1] == 802.6


def test_旧数据回填电力累计利润(db_session, sample_seed):
    db_session.add_all(
        [
            CoalMonthlyPoint(
                month="2025-02",
                output_10k_t=76533,
                output_cum_10k_t=76533,
                profit_cum_yi=506.6,
            ),
            CoalMonthlyPoint(
                month="2025-03",
                output_10k_t=44058,
                output_cum_10k_t=120260,
                profit_cum_yi=803.8,
            ),
        ]
    )
    db_session.commit()

    coal_monthly_service.get_curve_payload(
        db_session,
        coal_monthly_service.COAL_MONTHLY_GROUP_ID,
        90,
        full=True,
    )

    records = {
        record.month: record
        for record in db_session.query(CoalMonthlyPoint).all()
    }
    assert records["2025-02"].power_profit_cum_yi == 1123.2
    assert records["2025-03"].power_profit_cum_yi == 1777.4


def test_煤炭电力利润指标与序列(db_session, sample_seed):
    payload = coal_monthly_service.get_curve_payload(
        db_session,
        coal_monthly_service.COAL_POWER_GROUP_ID,
        90,
        full=True,
    )

    assert payload["group"] == "coalPowerProfit"
    assert payload["group_label"] == "煤炭·电力利润"
    assert payload["scope"] == "煤炭与电力行业月度利润"
    assert payload["dates"] == ["2025-02", "2025-03", "2025-04"]
    assert payload["series"]["coal_profit"] == [506.6, 297.2, 223.0]
    assert payload["series"]["power_profit"] == [1123.2, 654.2, 582.0]
    assert payload["series"]["power_profit_cum"] == [1123.2, 1777.4, 2359.4]
    assert payload["series"]["industry_profit_total_cum"] == [
        1629.8,
        2581.2,
        3386.2,
    ]

    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert metrics["coal_profit"]["latest"] == 223.0
    assert metrics["power_profit"]["latest"] == 582.0
    assert metrics["power_profit"]["delta"] == -72.2
    assert metrics["industry_profit_total"]["latest"] == 805.0
    assert payload["storage"]["table"] == "coal_monthly_points"


def test_未知分组仍被拒绝(db_session, sample_seed):
    with pytest.raises(ValueError):
        coal_monthly_service.get_curve_payload(db_session, "unknown", 90)


def test_目录包含煤炭月度分组():
    groups = {group["id"]: group for group in catalog_payload()["groups"]}
    group = groups["coalMonthly"]
    assert group["label"] == "煤炭产量·吨煤利润"
    assert group["available"] is True
    assert group["category"] == "commodity"
    power = groups["coalPowerProfit"]
    assert power["label"] == "煤炭·电力利润"
    assert power["available"] is True
    assert power["category"] == "commodity"


def test_路由分发到煤炭月度服务(api_client):
    response = api_client.get(
        "/api/etf-shares/curve?group=coalMonthly&days=90&full=1"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["group"] == "coalMonthly"
    assert payload["dates"] == ["2025-02", "2025-03", "2025-04"]
    assert "coal_profit_per_ton" in payload["series"]


def test_路由分发到煤炭电力利润(api_client):
    response = api_client.get(
        "/api/etf-shares/curve?group=coalPowerProfit&days=90&full=1"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["group"] == "coalPowerProfit"
    assert payload["dates"] == ["2025-02", "2025-03", "2025-04"]
    assert payload["series"]["power_profit"] == [1123.2, 654.2, 582.0]
