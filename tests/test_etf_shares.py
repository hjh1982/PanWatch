"""ETF 份额追踪的采集、缓存与曲线构建单元测试。"""

from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
from fastapi import FastAPI
from openpyxl import Workbook
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.modules.market import etf_share_service as service
from src.modules.market.api import etf_shares as etf_shares_api
from src.platform.persistence.database import Base, get_db
from src.platform.persistence.models import EtfShareSnapshot, EtfShareTradeDay


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db, code: str, trade_date: str, shares_yi: float, name: str = "300ETF"):
    db.add(
        EtfShareSnapshot(
            trade_date=trade_date,
            code=code,
            name=name,
            shares_yi=shares_yi,
        )
    )


def test_解析上交所份额并换算为亿份():
    """TOT_VOL 以万份返回，换算成亿份；未知代码和非法数值被忽略。"""
    payload = {
        "result": [
            {"SEC_CODE": "510300", "SEC_NAME": "300ETF", "TOT_VOL": "3019908.77"},
            {"SEC_CODE": "510310", "SEC_NAME": "HS300ETF", "TOT_VOL": "1706284.4"},
            {"SEC_CODE": "999999", "SEC_NAME": "OTHER", "TOT_VOL": "100"},
            {"SEC_CODE": "510330", "SEC_NAME": "华夏300", "TOT_VOL": None},
        ]
    }

    parsed = service.parse_sse_payload(payload)

    assert parsed is not None
    assert parsed.total == 4
    assert [row["code"] for row in parsed.rows] == ["510300", "510310"]
    assert parsed.rows[0]["shares_yi"] == pytest.approx(301.990877)
    assert service.parse_sse_payload({"result": "bad"}) is None


def _szse_workbook(rows: list[tuple]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(("日期", "基金代码", "基金简称", "基金规模(份)"))
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_解析深交所份额表并换算为亿份():
    """深交所报表份额单位为份，换算成亿份；未知代码和非法数值被忽略。"""
    content = _szse_workbook(
        [
            ("2026-10-30", "159949", "创业板50ETF华安", "23036789819.00"),
            ("2026/10/30", "159682", "创业板50ETF景顺", "3,409,308,000.00"),
            ("2026-10-30", "999999", "OTHER", "100"),
            ("2026-10-30", "159681", "创业板50ETF鹏华", None),
        ]
    )

    parsed = service.parse_szse_workbook(content)

    assert [row["code"] for row in parsed] == ["159949", "159682"]
    assert parsed[0]["trade_date"] == "2026-10-30"
    assert parsed[0]["shares_yi"] == pytest.approx(230.367898)
    assert parsed[1]["shares_yi"] == pytest.approx(34.09308)
    assert service.parse_szse_workbook(_szse_workbook([])) == []


def test_深交所分组按窗口抓取并避免重复请求(db, monkeypatch):
    """创业板50 一次抓取整个窗口；已覆盖后不再请求，缺口覆盖按基金计算。"""
    calls: list[tuple[str, str]] = []

    def fake_fetch(start: str, end: str):
        calls.append((start, end))
        return [
            {
                "trade_date": "2026-10-29",
                "code": "159949",
                "name": "创业板50ETF华安",
                "shares_yi": 225.0,
            },
            {
                "trade_date": "2026-10-30",
                "code": "159949",
                "name": "创业板50ETF华安",
                "shares_yi": 230.0,
            },
        ]

    monkeypatch.setattr(service, "fetch_szse_range", fake_fetch)
    group = service._require_group("cyb50")
    targets = ["2026-10-29", "2026-10-30"]

    service._sync_szse_for_response(db, group, targets, force=False)
    service._sync_szse_for_response(db, group, targets, force=False)

    assert calls == [("2026-10-29", "2026-10-30")]
    payload = service.build_curve_payload(db, "cyb50", 30, targets=targets)
    assert payload["dates"] == targets
    assert payload["coverage"] == {
        "trading_days": 2,
        "target": 2,
        "complete": True,
    }
    fund = payload["funds"][0]
    assert fund["latest"] == pytest.approx(230.0)
    assert fund["delta"] == pytest.approx(5.0)


def test_深交所长窗口分段抓取并避免重复请求(db, monkeypatch):
    """250 个交易日超出深交所单次上限，按约 100 天分段；全部覆盖后不再请求。"""
    calls: list[tuple[str, str]] = []
    targets = service.target_trade_dates(250, end=date(2026, 11, 2))

    def fake_fetch(start: str, end: str):
        calls.append((start, end))
        return [
            {
                "trade_date": day,
                "code": "159949",
                "name": "创业板50ETF华安",
                "shares_yi": 100.0,
            }
            for day in targets
            if start <= day <= end
        ]

    monkeypatch.setattr(service, "fetch_szse_range", fake_fetch)
    group = service._require_group("cyb50")

    service._sync_szse_for_response(db, group, targets, force=False)
    chunk_calls = len(calls)
    service._sync_szse_for_response(db, group, targets, force=False)

    assert chunk_calls > 1
    assert len(calls) == chunk_calls
    assert all(
        (date.fromisoformat(end) - date.fromisoformat(start)).days < 100
        for start, end in calls
    )
    payload = service.build_curve_payload(db, "cyb50", 250, targets=targets)
    assert payload["coverage"] == {
        "trading_days": 250,
        "target": 250,
        "complete": True,
    }
    assert payload["funds"][0]["values"][0] == pytest.approx(100.0)


def test_目标交易日排除周末和法定节假日():
    """近 5 个交易日止于 10 月 9 日，国庆假期和周末不进入曲线。"""
    days = service.target_trade_dates(5, end=date(2026, 10, 9))

    assert days == [
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
        "2026-10-08",
        "2026-10-09",
    ]


def test_曲线包含最新份额_环比变化与缺失值(db):
    """卡片数据取窗口内最新值与上一可用交易日，缺失日期保留空值。"""
    for day in ("2026-10-19", "2026-10-20", "2026-10-21"):
        db.add(EtfShareTradeDay(trade_date=day, total=924))
    _seed(db, "510300", "2026-10-19", 100.0)
    _seed(db, "510300", "2026-10-21", 105.0, name="300ETF")
    _seed(db, "510310", "2026-10-19", 50.0, name="HS300ETF")
    _seed(db, "510310", "2026-10-21", 49.5, name="HS300ETF")
    db.commit()

    payload = service.build_curve_payload(
        db,
        "hs300",
        90,
        targets=["2026-10-19", "2026-10-20", "2026-10-21"],
    )

    assert payload["as_of"] == "2026-10-21"
    assert payload["dates"] == ["2026-10-19", "2026-10-21"]
    # 2026-10-20 没有任何基金份额，按分组口径仍未完整。
    assert payload["coverage"] == {
        "trading_days": 2,
        "target": 3,
        "complete": False,
    }
    fund = next(item for item in payload["funds"] if item["code"] == "510300")
    assert fund["name"] == "沪深300ETF华泰柏瑞"
    assert fund["latest"] == pytest.approx(105.0)
    assert fund["delta"] == pytest.approx(5.0)
    assert fund["delta_pct"] == pytest.approx(5.0)
    assert fund["values"] == [100.0, 105.0]
    fallback = next(item for item in payload["funds"] if item["code"] == "510310")
    assert fallback["delta"] == pytest.approx(-0.5)


def test_历史交易日未同步时标记为未完成(db):
    """已取到份额但交易日尚未全部落库时，前端应继续轮询补齐。"""
    for day in ("2026-10-19", "2026-10-21"):
        _seed(db, "510300", day, 100.0)
    db.add(EtfShareTradeDay(trade_date="2026-10-19", total=924))
    db.add(EtfShareTradeDay(trade_date="2026-10-21", total=924))
    db.commit()

    payload = service.build_curve_payload(
        db,
        "hs300",
        90,
        targets=["2026-10-19", "2026-10-20", "2026-10-21"],
    )

    assert payload["coverage"]["complete"] is False


def test_抓取单日数据写库后不再重复请求(db, monkeypatch):
    """成功抓取的单日会写入份额与交易日记录，后续同步直接跳过。"""
    calls: list[str] = []

    def fake_fetch(trade_date: str):
        calls.append(trade_date)
        return service.DayFetch(
            rows=[{"code": "510300", "name": "300ETF", "shares_yi": 301.99}],
            total=924,
        )

    monkeypatch.setattr(service, "fetch_sse_day", fake_fetch)
    monkeypatch.setattr(service, "_last_attempt", {})

    assert service._sync_day(db, "2026-10-21") is True
    assert service._sync_day(db, "2026-10-21") is False

    assert calls == ["2026-10-21"]
    assert db.get(EtfShareTradeDay, "2026-10-21").total == 924
    assert db.query(EtfShareSnapshot).count() == 1


def test_当日尚未披露时不记录零行(monkeypatch):
    """当天 15:00 后可能仍在清算，零行不能固化成已同步历史。"""
    today = service.today_cn().isoformat()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    monkeypatch.setattr(
        service,
        "fetch_sse_day",
        lambda _day: service.DayFetch(rows=[], total=0),
    )
    monkeypatch.setattr(service, "_last_attempt", {})
    try:
        assert service._sync_day(session, today, force=True) is False
        assert session.get(EtfShareTradeDay, today) is None
    finally:
        session.close()
        engine.dispose()


def test_目录返回完整页签结构():
    """份额、融资融券、煤炭与电解铝可接入，其余参考页签保留为待接入占位。"""
    payload = service.catalog_payload()

    assert payload["default_group"] == "hs300"
    assert payload["default_days"] in payload["allowed_days"]
    assert payload["allowed_days"] == [30, 60, 90, 120, 250]
    groups = {group["id"]: group for group in payload["groups"]}
    assert set(groups) == {
        "hs300",
        "zz500",
        "cyb50",
        "kc50",
        "margin",
        "usCompare",
        "coalPrice",
        "coalLtc",
        "coalMonthly",
        "coalPowerProfit",
        "globalCoal",
        "autoSales",
        "usM2Gold",
        "dividendYield",
        "aluminum",
        "aluminumInventory",
        "goldReserves",
        "scfi",
        "hydropower",
        "threeGorges",
    }
    assert [fund["code"] for fund in groups["hs300"]["funds"]] == [
        "510300",
        "510310",
        "510320",
        "510330",
        "510350",
    ]
    assert len(groups["zz500"]["funds"]) == 5
    assert [fund["code"] for fund in groups["cyb50"]["funds"]] == [
        "159949",
        "159682",
        "159681",
        "159369",
        "159375",
    ]
    assert [fund["code"] for fund in groups["kc50"]["funds"]] == [
        "588000",
        "588080",
        "588050",
        "588090",
        "588180",
    ]
    assert groups["hs300"]["available"] is True
    assert groups["zz500"]["available"] is True
    assert groups["cyb50"]["available"] is True
    assert groups["kc50"]["available"] is True
    assert groups["margin"]["available"] is True
    assert groups["margin"]["category"] == "broad"
    assert groups["margin"]["funds"] == []
    assert groups["usCompare"]["available"] is True
    assert groups["usCompare"]["category"] == "broad"
    assert groups["usCompare"]["funds"] == []
    assert groups["coalPrice"]["available"] is True
    assert groups["coalPrice"]["category"] == "commodity"
    assert groups["coalPrice"]["funds"] == []
    assert groups["coalMonthly"]["available"] is True
    assert groups["coalMonthly"]["category"] == "commodity"
    assert groups["coalMonthly"]["funds"] == []
    assert groups["coalPowerProfit"]["available"] is True
    assert groups["coalPowerProfit"]["category"] == "commodity"
    assert groups["coalPowerProfit"]["funds"] == []
    assert groups["globalCoal"]["available"] is True
    assert groups["globalCoal"]["category"] == "commodity"
    assert groups["autoSales"]["available"] is True
    assert groups["autoSales"]["category"] == "industry"
    assert groups["autoSales"]["funds"] == []
    assert groups["usM2Gold"]["available"] is True
    assert groups["usM2Gold"]["category"] == "macro"
    assert groups["usM2Gold"]["funds"] == []
    assert groups["dividendYield"]["available"] is True
    assert groups["dividendYield"]["category"] == "macro"
    assert groups["dividendYield"]["funds"] == []
    assert groups["aluminum"]["available"] is True
    assert groups["aluminum"]["category"] == "commodity"
    assert groups["aluminum"]["funds"] == []
    assert groups["aluminumInventory"]["available"] is True
    assert groups["aluminumInventory"]["category"] == "commodity"
    assert groups["goldReserves"]["available"] is True
    assert groups["goldReserves"]["category"] == "commodity"
    assert groups["goldReserves"]["funds"] == []
    assert groups["scfi"]["available"] is True
    assert groups["scfi"]["category"] == "commodity"
    assert groups["scfi"]["funds"] == []
    assert groups["hydropower"]["available"] is True
    assert groups["hydropower"]["category"] == "commodity"
    assert groups["hydropower"]["funds"] == []
    assert groups["threeGorges"]["available"] is True
    assert groups["threeGorges"]["category"] == "commodity"
    assert groups["threeGorges"]["funds"] == []


def test_目录接口可由前端直接调用():
    """catalog 路由返回分组与可选区间，不依赖数据库。"""
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")

    response = TestClient(app).get("/api/etf-shares/catalog")

    assert response.status_code == 200
    assert [group["id"] for group in response.json()["groups"]] == [
        "hs300",
        "zz500",
        "cyb50",
        "kc50",
        "margin",
        "usCompare",
        "coalPrice",
        "coalLtc",
        "coalMonthly",
        "coalPowerProfit",
        "globalCoal",
        "autoSales",
        "usM2Gold",
        "dividendYield",
        "aluminum",
        "aluminumInventory",
        "goldReserves",
        "scfi",
        "hydropower",
        "threeGorges",
    ]


def test_曲线接口校验未知分组(db):
    """未知分组返回 400，避免前端误以为得到空数据。"""
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: db

    response = TestClient(app).get("/api/etf-shares/curve?group=unknown")

    assert response.status_code == 400
