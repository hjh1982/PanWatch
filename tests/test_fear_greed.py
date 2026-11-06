"""A 股恐贪指数：解析、阈值、缓存降级与路由测试。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.modules.market import fear_greed_service
from src.modules.market.api import market as market_api
from src.platform.persistence.database import Base, get_db
from src.platform.persistence.models import FearGreedPoint

_PAYLOAD = {
    "range": "1",
    "mode": "sampled",
    "generated_at": "2026-10-24T14:00:34",
    "points": [
        ["2026-10-21", 36.63, 11704.35],
        ["2026-10-20", 40.31, 11801.29],
        ["2026-10-22", 25.98, 11357.32],
        ["2026-10-23", 51.64, 11673.83],
        ["2026-10-24", None, 11673.83],
        ["2026-10-25", 120, 11673.83],
        ["bad-date", 50, 1],
    ],
}


@pytest.fixture(autouse=True)
def reset_state():
    fear_greed_service._state["last_attempt"] = 0.0
    fear_greed_service._state["generated_at"] = ""
    yield
    fear_greed_service._state["last_attempt"] = 0.0
    fear_greed_service._state["generated_at"] = ""


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


def test_解析读数时排序并过滤无效点():
    rows, generated_at = fear_greed_service.parse_payload(_PAYLOAD)

    assert [row.trade_date for row in rows] == [
        "2026-10-20",
        "2026-10-21",
        "2026-10-22",
        "2026-10-23",
    ]
    assert rows[-1].score == 51.64
    assert rows[-1].shenzhen_index == 11673.83
    assert generated_at == "2026-10-24T14:00:34"


@pytest.mark.parametrize(
    ("score", "level"),
    [
        (0, "extreme_fear"),
        (25, "extreme_fear"),
        (25.01, "fear"),
        (45, "fear"),
        (45.01, "neutral"),
        (55, "neutral"),
        (55.01, "greed"),
        (75, "greed"),
        (75.01, "extreme_greed"),
        (100, "extreme_greed"),
    ],
)
def test_情绪阈值与数据源口径一致(score, level):
    assert fear_greed_service.level_for(score) == level


def test_payload包含最新读数_日变化与历史窗口():
    rows, _ = fear_greed_service.parse_payload(_PAYLOAD)

    payload = fear_greed_service.build_payload(rows, 3)

    assert payload["date"] == "2026-10-23"
    assert payload["score"] == 51.64
    assert payload["level"] == "neutral"
    assert payload["change"] == 25.66
    assert payload["shenzhen_index"] == 11673.83
    assert payload["history"] == [
        {"date": "2026-10-21", "score": 36.63},
        {"date": "2026-10-22", "score": 25.98},
        {"date": "2026-10-23", "score": 51.64},
    ]
    assert payload["source"]["generated_at"] is None


def test_同步写入数据库并读取最新读数(db_session, monkeypatch):
    monkeypatch.setattr(fear_greed_service, "market_get", lambda *a, **k: _PAYLOAD)

    payload = fear_greed_service.get_payload(db_session, 30, force=True)

    assert payload["score"] == 51.64
    assert fear_greed_service._state["generated_at"] == "2026-10-24T14:00:34"
    records = db_session.query(FearGreedPoint).order_by(FearGreedPoint.trade_date).all()
    assert [record.trade_date for record in records] == [
        "2026-10-20",
        "2026-10-21",
        "2026-10-22",
        "2026-10-23",
    ]


def test_数据源失败时回退本地缓存(db_session, monkeypatch):
    db_session.add(FearGreedPoint(trade_date="2026-10-22", score=25.98))
    db_session.add(FearGreedPoint(trade_date="2026-10-23", score=51.64))
    db_session.commit()
    monkeypatch.setattr(fear_greed_service, "market_get", lambda *a, **k: None)

    payload = fear_greed_service.get_payload(db_session, 30, force=True)

    assert payload["score"] == 51.64
    assert payload["change"] == 25.66
    assert payload["source"]["generated_at"] is None


def _client(db_session):
    app = FastAPI()
    app.include_router(market_api.router, prefix="/api/market")
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_恐贪指数接口返回最新读数(db_session, monkeypatch):
    db_session.add(FearGreedPoint(trade_date="2026-10-22", score=25.98))
    db_session.add(FearGreedPoint(trade_date="2026-10-23", score=51.64, shenzhen_index=11673.83))
    db_session.commit()
    monkeypatch.setattr(fear_greed_service, "_maybe_sync", lambda *a, **k: None)

    response = _client(db_session).get("/api/market/fear-greed?days=30")

    assert response.status_code == 200
    payload = response.json()
    assert payload["score"] == 51.64
    assert payload["level"] == "neutral"
    assert payload["history"][-1]["date"] == "2026-10-23"


def test_无缓存且数据源不可用时返回503(db_session, monkeypatch):
    monkeypatch.setattr(fear_greed_service, "_maybe_sync", lambda *a, **k: None)

    response = _client(db_session).get("/api/market/fear-greed")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "fear_greed_unavailable"
