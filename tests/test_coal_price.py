"""煤炭价格：CCI5500 日度价格的解析、窗口、持久化与路由分发测试。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.market import coal_price_service
from src.modules.market.api import etf_shares as etf_shares_api
from src.platform.persistence.database import Base, get_db
from src.platform.persistence.models import CoalPricePoint  # noqa: F401 - 注册表结构

_SAMPLE_CSV = """date,cci_5500
2026-10-14,1018
2026-10-13,1013
2026-10-12,1008
2026-10-10,1003
2026-10-09,998
2026-10-08,993
2026-09-24,991
"""

_SAMPLE_ROWS = {"2026-10-13": 1013, "2026-10-14": 1018}


def _write_csv(tmp_path, content: str = _SAMPLE_CSV):
    path = tmp_path / "coal_cci_5500.csv"
    path.write_text(content, encoding="utf-8")
    return path


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
        coal_price_service,
        "load_cci_rows",
        lambda path=None: dict(_SAMPLE_ROWS),
    )


@pytest.fixture
def api_client(db_session, sample_seed):
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_CCI日度价格按日期对齐并计算环比(tmp_path, monkeypatch):
    monkeypatch.setattr(coal_price_service, "today_cn", lambda: date(2026, 10, 22))
    rows = coal_price_service.load_cci_rows(_write_csv(tmp_path))
    payload = coal_price_service.build_price_payload(rows, 60)

    assert payload["group"] == "coalPrice"
    assert payload["group_label"] == "煤炭价格"
    assert payload["scope"] == "CCI5500"
    assert payload["dates"] == [
        "2026-09-24",
        "2026-10-08",
        "2026-10-09",
        "2026-10-10",
        "2026-10-12",
        "2026-10-13",
        "2026-10-14",
    ]
    assert payload["as_of"] == "2026-10-14"
    assert payload["coverage"] == {
        "trading_days": 7,
        "target": 7,
        "complete": True,
    }
    assert payload["metrics"] == [
        {
            "id": "cci_5500",
            "latest": 1018.0,
            "latest_date": "2026-10-14",
            "delta": 5.0,
            "delta_pct": 0.4936,
        }
    ]
    assert payload["series"]["cci_5500"] == [
        991,
        993,
        998,
        1003,
        1008,
        1013,
        1018,
    ]
    assert Path(payload["storage"]["database"]).name == "panwatch.db"
    assert payload["storage"]["table"] == "coal_price_points"
    assert Path(payload["storage"]["seed"]).name == "coal_cci_5500.csv"


def test_价格窗口按自然日截取(tmp_path, monkeypatch):
    monkeypatch.setattr(coal_price_service, "today_cn", lambda: date(2026, 10, 22))
    rows = coal_price_service.load_cci_rows(_write_csv(tmp_path))
    payload = coal_price_service.build_price_payload(rows, 10)

    assert payload["dates"] == ["2026-10-12", "2026-10-13", "2026-10-14"]
    assert payload["series"]["cci_5500"] == [1008, 1013, 1018]
    assert payload["metrics"][0]["delta"] == 5


def test_全量模式忽略时间窗口(tmp_path, monkeypatch):
    monkeypatch.setattr(coal_price_service, "today_cn", lambda: date(2026, 10, 22))
    rows = coal_price_service.load_cci_rows(
        _write_csv(
            tmp_path,
            """date,cci_5500
2025-01-02,900
2026-10-13,1013
2026-10-14,1018
""",
        )
    )
    payload = coal_price_service.build_price_payload(rows, 10, full=True)

    assert payload["dates"] == ["2025-01-02", "2026-10-13", "2026-10-14"]
    assert payload["series"]["cci_5500"] == [900, 1013, 1018]
    assert payload["coverage"]["trading_days"] == 3


def test_解析时忽略空值与非法日期(tmp_path):
    rows = coal_price_service.load_cci_rows(
        _write_csv(
            tmp_path,
            """date,cci_5500
2026-10-14,1018
2026-10-13,
2026-10-12,-
2026-10-11,1010
bad-date,999
2026-13-01,1001
""",
        )
    )

    assert rows == {"2026-10-11": 1010, "2026-10-14": 1018}


def test_数据文件缺失时返回空曲线(tmp_path):
    rows = coal_price_service.load_cci_rows(tmp_path / "missing.csv")
    payload = coal_price_service.build_price_payload(rows, 365)

    assert rows == {}
    assert payload["metrics"] == []
    assert payload["dates"] == []
    assert payload["as_of"] is None
    assert payload["coverage"]["complete"] is False


def test_数据表为空时用内置CSV初始化且只初始化一次(db_session, monkeypatch):
    monkeypatch.setattr(coal_price_service, "today_cn", lambda: date(2026, 10, 22))

    payload = coal_price_service.get_curve_payload(db_session, "coalPrice", 365)
    count = db_session.query(CoalPricePoint).count()

    assert count > 400
    assert payload["scope"] == "CCI5500"
    assert payload["metrics"][0]["latest"] == 1018
    assert payload["metrics"][0]["latest_date"] == "2026-10-14"
    assert payload["dates"][-1] == "2026-10-14"
    assert payload["dates"].__len__() == len(payload["series"]["cci_5500"])

    coal_price_service.get_curve_payload(db_session, "coalPrice", 365)
    assert db_session.query(CoalPricePoint).count() == count


def test_新增煤炭数据写入数据库(db_session, sample_seed):
    assert set(coal_price_service.load_price_rows(db_session)) == set(_SAMPLE_ROWS)

    created = coal_price_service.create_point(db_session, "2026-10-15", 1020)

    assert created == {"date": "2026-10-15", "price": 1020.0}
    assert coal_price_service.load_price_rows(db_session) == {
        **{day: float(value) for day, value in _SAMPLE_ROWS.items()},
        "2026-10-15": 1020.0,
    }


def test_新增重复日期时报冲突(db_session, sample_seed):
    coal_price_service.create_point(db_session, "2026-10-15", 1020)

    with pytest.raises(coal_price_service.CoalPriceDateExists):
        coal_price_service.create_point(db_session, "2026-10-15", 1021)


def test_编辑煤炭数据的日期与价格(db_session, sample_seed):
    coal_price_service.create_point(db_session, "2026-10-15", 1020)

    updated = coal_price_service.update_point(
        db_session, "2026-10-15", "2026-10-16", 1025
    )

    assert updated == {"date": "2026-10-16", "price": 1025.0}
    rows = coal_price_service.load_price_rows(db_session)
    assert "2026-10-15" not in rows
    assert rows["2026-10-16"] == 1025.0


def test_编辑为已有日期时报冲突(db_session, sample_seed):
    coal_price_service.load_price_rows(db_session)
    coal_price_service.create_point(db_session, "2026-10-15", 1020)

    with pytest.raises(coal_price_service.CoalPriceDateExists):
        coal_price_service.update_point(
            db_session, "2026-10-15", "2026-10-14", 1025
        )


def test_编辑不存在的日期时报未找到(db_session, sample_seed):
    with pytest.raises(coal_price_service.CoalPriceNotFound):
        coal_price_service.update_point(
            db_session, "2026-10-15", "2026-10-15", 1025
        )


@pytest.mark.parametrize(
    "raw_day,price,error",
    [
        ("2026/10/15", 1000, coal_price_service.InvalidCoalDate),
        ("2026-02-30", 1000, coal_price_service.InvalidCoalDate),
        ("2026-10-15", 0, coal_price_service.InvalidCoalPrice),
        ("2026-10-15", -1, coal_price_service.InvalidCoalPrice),
        ("2026-10-15", 1000.5, coal_price_service.InvalidCoalPrice),
    ],
)
def test_非法日期或价格被拒绝(db_session, raw_day, price, error):
    with pytest.raises(error):
        coal_price_service.create_point(db_session, raw_day, price)


def test_煤炭数据接口支持新增与编辑(api_client, monkeypatch):
    monkeypatch.setattr(coal_price_service, "today_cn", lambda: date(2026, 10, 22))
    assert api_client.get(
        "/api/etf-shares/curve?group=coalPrice&days=365"
    ).status_code == 200

    created = api_client.post(
        "/api/etf-shares/coal/points",
        json={"date": "2026-10-15", "price": 1020},
    )
    assert created.status_code == 200
    assert created.json() == {"date": "2026-10-15", "price": 1020}

    updated = api_client.put(
        "/api/etf-shares/coal/points/2026-10-15",
        json={"date": "2026-10-16", "price": 1025},
    )
    assert updated.status_code == 200
    assert updated.json() == {"date": "2026-10-16", "price": 1025}

    curve = api_client.get("/api/etf-shares/curve?group=coalPrice&days=365").json()
    assert curve["dates"][-2:] == ["2026-10-14", "2026-10-16"]
    assert curve["series"]["cci_5500"][-2:] == [1018, 1025]
    assert curve["metrics"][0]["latest"] == 1025


def test_煤炭数据接口返回稳定错误码(api_client):
    assert (
        api_client.post(
            "/api/etf-shares/coal/points",
            json={"date": "2026-10-15", "price": 1020},
        ).status_code
        == 200
    )

    duplicate = api_client.post(
        "/api/etf-shares/coal/points",
        json={"date": "2026-10-15", "price": 1021},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "coal_price_date_exists"

    bad_day = api_client.post(
        "/api/etf-shares/coal/points",
        json={"date": "2026/10/16", "price": 1021},
    )
    assert bad_day.status_code == 400
    assert bad_day.json()["detail"]["code"] == "coal_price_date_invalid"

    bad_price = api_client.post(
        "/api/etf-shares/coal/points",
        json={"date": "2026-10-16", "price": 10.5},
    )
    assert bad_price.status_code == 400
    assert bad_price.json()["detail"]["code"] == "coal_price_value_invalid"

    missing = api_client.put(
        "/api/etf-shares/coal/points/2026-10-16",
        json={"date": "2026-10-16", "price": 1021},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "coal_price_not_found"


def test_曲线接口全量模式返回所有年份(api_client, monkeypatch):
    monkeypatch.setattr(coal_price_service, "today_cn", lambda: date(2026, 10, 22))
    monkeypatch.setattr(
        coal_price_service,
        "load_cci_rows",
        lambda path=None: {
            "2025-01-02": 900.0,
            "2026-10-13": 1013.0,
            "2026-10-14": 1018.0,
        },
    )

    limited = api_client.get(
        "/api/etf-shares/curve?group=coalPrice&days=10"
    ).json()
    full = api_client.get(
        "/api/etf-shares/curve?group=coalPrice&days=10&full=1"
    ).json()

    assert limited["dates"] == ["2026-10-13", "2026-10-14"]
    assert full["dates"] == ["2025-01-02", "2026-10-13", "2026-10-14"]


def test_曲线接口把煤炭分组分发给专用服务(monkeypatch):
    calls: list[tuple[object, str, int, bool, bool]] = []

    def fake_curve(
        db, group: str, days: int, *, force: bool = False, full: bool = False
    ):
        calls.append((db, group, days, force, full))
        return {
            "group": "coalPrice",
            "group_label": "煤炭价格",
            "scope": "CCI5500",
            "as_of": "2026-10-14",
            "dates": [],
            "metrics": [],
            "series": {},
            "coverage": {"trading_days": 0, "target": 0, "complete": True},
        }

    monkeypatch.setattr(coal_price_service, "get_curve_payload", fake_curve)
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)

    assert client.get(
        "/api/etf-shares/curve?group=coalPrice&days=365&full=1"
    ).status_code == 200
    assert calls[-1][1:] == ("coalPrice", 365, False, True)

    assert client.post(
        "/api/etf-shares/refresh?group=coalPrice&days=730"
    ).status_code == 200
    assert calls[-1][1:] == ("coalPrice", 730, True, False)
