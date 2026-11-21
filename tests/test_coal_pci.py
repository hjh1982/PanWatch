"""喷吹煤价格：钢之家现货页解析、窗口与路由分发测试。"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.market import coal_pci_service
from src.modules.market.api import etf_shares as etf_shares_api
from src.platform.persistence.database import get_db


def _sample_items() -> list[dict]:
    return [
        {
            "price_name": "长治",
            "price_id": "K163304",
            "price": "1330",
            "xAxis": ["2026-11-20", "2026-11-19", "2026-11-18"],
            "yAxis": ["1330", "1330", "1320"],
        },
        {
            "price_name": "晋城",
            "price_id": "M166208",
            "price": "1300",
            "xAxis": ["2026-11-20", "2026-11-19", "2026-11-18"],
            "yAxis": ["1300", "1280", "1290"],
        },
        # 指数块同样叫“长治”，但没有 price_id，不能与现货序列混淆
        {"price_name": "长治", "price": 1348},
        {"price_id": "X1", "xAxis": ["2026-11-20"], "yAxis": ["1"]},
    ]


def _sample_html() -> str:
    payload = json.dumps(_sample_items(), ensure_ascii=False)
    return (
        "<script>let spotQuotationList = '"
        + payload
        + "';\nspotQuotationList = JSON.parse(spotQuotationList);</script>"
    )


def test_解析现货页并只取晋城长治():
    rows = coal_pci_service.parse_payload(_sample_html())

    assert set(rows) == {"jincheng", "changzhi"}
    assert rows["jincheng"] == {
        "2026-11-18": 1290.0,
        "2026-11-19": 1280.0,
        "2026-11-20": 1300.0,
    }
    assert rows["changzhi"]["2026-11-20"] == 1330.0
    assert rows["changzhi"]["2026-11-18"] == 1320.0
    # 兼容直接传入 JSON 数组
    assert coal_pci_service.parse_payload(json.dumps(_sample_items())) == rows


def test_解析空响应与过滤异常点():
    assert coal_pci_service.parse_payload("") == {}
    assert coal_pci_service.parse_payload("<html>no data</html>") == {}

    items = [
        {
            "price_id": "M166208",
            "xAxis": ["2026-11-20", "bad-date", "2026-11-21"],
            "yAxis": ["0", "100", "abc"],
        }
    ]
    assert coal_pci_service.parse_payload(json.dumps(items)) == {}


def test_构建曲线按窗口对齐并计算环比():
    rows = coal_pci_service.parse_payload(_sample_html())
    payload = coal_pci_service.build_payload(rows, 2)

    assert payload["group"] == "coalPci"
    assert payload["group_label"] == "喷吹煤价格"
    assert payload["dates"] == ["2026-11-19", "2026-11-20"]
    assert payload["as_of"] == "2026-11-20"
    assert payload["series"]["jincheng"] == [1280.0, 1300.0]
    assert payload["series"]["changzhi"] == [1330.0, 1330.0]

    metrics = {metric["id"]: metric for metric in payload["metrics"]}
    assert metrics["jincheng"]["latest"] == 1300.0
    assert metrics["jincheng"]["delta"] == 20.0
    assert metrics["jincheng"]["delta_pct"] == 1.5625
    assert metrics["changzhi"]["delta"] == 0.0
    assert metrics["changzhi"]["delta_pct"] == 0.0
    assert payload["coverage"] == {
        "trading_days": 2,
        "target": 2,
        "complete": True,
    }
    assert payload["sources"][0]["id"] == "steelhome_pci"
    assert payload["sources"][0]["latest"] == "2026-11-20"


def test_缺失序列时曲线补空且完整度为假():
    rows = {"jincheng": {"2026-11-19": 1300.0, "2026-11-20": 1300.0}}
    payload = coal_pci_service.build_payload(rows, 2)

    assert len(payload["metrics"]) == 1
    assert payload["series"]["changzhi"] == [None, None]
    assert payload["coverage"]["complete"] is False


def test_只跟踪晋城与长治两条现货序列():
    assert [(item.id, item.price_id) for item in coal_pci_service.SERIES] == [
        ("jincheng", "M166208"),
        ("changzhi", "K163304"),
    ]
    assert coal_pci_service._BY_PRICE_ID["K163304"].id == "changzhi"


def test_未知分组被拒绝():
    with pytest.raises(ValueError):
        coal_pci_service.get_curve_payload("not-a-group", 90)


def _mysteel_payload() -> dict:
    body = [
        {
            "indexCode": "ID01360504",
            "indexName": "无烟喷吹煤：精煤：晋城市：兰花（日度）",
            "xAxis": ["2026-11-20", "2025-11-20", "2025-11-19"],
            "datas": [{"indexCode": "ID01360504", "yAxis": ["1300", "985", "980"]}],
        }
    ]
    return {"response": json.dumps(body, ensure_ascii=False)}


def test_解析我的钢铁网晋城指数():
    rows = coal_pci_service.parse_mysteel_index(_mysteel_payload())

    assert rows == {
        "2025-11-19": 980.0,
        "2025-11-20": 985.0,
        "2026-11-20": 1300.0,
    }
    assert coal_pci_service.parse_mysteel_index({"response": "bad"}) == {}


def test_解析长治日评同比并换算去年同期价():
    text = (
        '<script type="application/ld+json">{"@type":"NewsArticle",'
        '"datePublished":"2026-11-20T08:48:48+08:00",'
        '"description":"2026-11-20 无烟喷吹精煤 Q>6500.0 长治 潞安古城矿 '
        "价格1350元/吨（Q6500），环比上日持平，上周同期1450元/吨，"
        '同比下跌100元/吨。"}</script>'
    )

    summary = coal_pci_service.parse_changzhi_summary(text)

    assert summary == {
        "date": "2026-11-20",
        "price": 1350.0,
        "year_ago": 1450.0,
        "yoy": -100.0,
        "yoy_pct": -6.8966,
    }
    assert coal_pci_service.parse_changzhi_summary("<html></html>") is None


def test_长治日评同比持平时为0():
    text = (
        '<script type="application/ld+json">{"datePublished":"2026-11-20",'
        '"description":"长治 潞安古城矿 价格1350元/吨，同比持平。"}</script>'
    )

    summary = coal_pci_service.parse_changzhi_summary(text)

    assert summary is not None
    assert summary["year_ago"] == 1350.0
    assert summary["yoy"] == 0.0
    assert summary["yoy_pct"] == 0.0


def test_构建曲线时附带同比且按去年同期取价():
    rows = coal_pci_service.parse_payload(_sample_html())
    yoy = {
        "jincheng": {"2025-11-20": 985.0},
        "changzhi": {
            "date": "2026-11-20",
            "year_ago": 1450.0,
            "yoy": -100.0,
            "yoy_pct": -6.8966,
        },
    }

    payload = coal_pci_service.build_payload(rows, 2, yoy)
    metrics = {metric["id"]: metric for metric in payload["metrics"]}

    assert metrics["jincheng"]["year_ago"] == 985.0
    assert metrics["jincheng"]["yoy"] == 315.0
    assert metrics["jincheng"]["yoy_pct"] == 31.9797
    assert metrics["jincheng"]["yoy_date"] == "2025-11-20"
    assert metrics["changzhi"]["year_ago"] == 1450.0
    assert metrics["changzhi"]["yoy"] == -120.0
    assert metrics["changzhi"]["yoy_pct"] == -8.2759
    assert [source["id"] for source in payload["sources"]] == [
        "steelhome_pci",
        "mysteel_pci_jincheng",
        "mysteel_pci_changzhi",
    ]


def test_同比缺失时不污染指标():
    rows = coal_pci_service.parse_payload(_sample_html())
    payload = coal_pci_service.build_payload(rows, 2, {})

    for metric in payload["metrics"]:
        assert "yoy_pct" not in metric
    assert [source["id"] for source in payload["sources"]] == ["steelhome_pci"]


def test_曲线接口把喷吹煤分组分发给专用服务(monkeypatch):
    calls: list[tuple[str, int, bool]] = []

    def fake_curve(group: str, days: int, *, force: bool = False):
        calls.append((group, days, force))
        return {
            "group": "coalPci",
            "group_label": "喷吹煤价格",
            "scope": "钢之家 喷吹煤现货价格 · 晋城/长治",
            "as_of": "2026-11-20",
            "dates": [],
            "metrics": [],
            "series": {},
            "coverage": {"trading_days": 0, "target": 0, "complete": True},
        }

    monkeypatch.setattr(coal_pci_service, "get_curve_payload", fake_curve)
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)

    assert client.get(
        "/api/etf-shares/curve?group=coalPci&days=90"
    ).status_code == 200
    assert calls[-1] == ("coalPci", 90, False)

    assert client.post(
        "/api/etf-shares/refresh?group=coalPci&days=250"
    ).status_code == 200
    assert calls[-1] == ("coalPci", 250, True)
