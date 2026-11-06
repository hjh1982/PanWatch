"""黄金储备：外管局年度表格解析、吨折算、payload 与接口。"""

from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.market import gold_reserves_service as service
from src.modules.market.api import etf_shares as etf_shares_api
from src.modules.market.statistics_store import StatisticsStore
from src.platform.persistence.database import get_db


def table_html(year: int, months: list[int], quantities: list[int]) -> str:
    header = "<tr><td>项目 Item</td>" + "".join(
        f"<td>{year}.{month:02d}</td>" for month in months
    ) + "</tr>"
    subheader = "<tr><td></td>" + "".join(
        "<td>亿美元</td><td>亿SDR</td>" for _ in months
    ) + "</tr>"
    gold = "<tr><td>4.  黄金 Gold</td>" + "".join(
        "<td>2065.34</td><td>1584.06</td>" for _ in months
    ) + "</tr>"
    quantity_cells = "".join(
        f"<td>{quantity}万盎司</td><td>{quantity}万盎司</td>"
        for quantity in quantities
    )
    quantity = f"<tr><td></td>{quantity_cells}</tr>"
    return f"<table>{header}{subheader}{gold}{quantity}</table>"


def test_年度表解析去掉美元与SDR重复列():
    html = table_html(2026, list(range(1, 13)), list(range(7400, 7412)))

    rows = service.parse_gold_table(html, 2026)

    assert list(rows) == [f"2026-{month:02d}" for month in range(1, 13)]
    assert rows["2026-01"] == 7400
    assert rows["2026-12"] == 7411


def test_年度表解析丢弃非目标年份():
    html = table_html(2018, list(range(1, 13)), [5924] * 12)

    assert service.parse_gold_table(html, 2019) == {}
    assert service.parse_gold_table(html, 2018)["2018-01"] == 5924


def test_年度表解析缺少数值行返回空():
    html = "<table><tr><td>项目 Item</td>" + "".join(
        f"<td>2025.{month:02d}</td>" for month in range(1, 13)
    ) + "</tr></table>"

    assert service.parse_gold_table(html, 2025) == {}
    assert service.parse_gold_table(None, 2025) == {}


def test_年度索引提取纯年份链接并升序():
    html = (
        '<a href="/safe/2025/0206/25745.html" title="官方储备资产（2025年）">'
        "2025</a>"
        '<a href="/safe/2026/0205/27113.html" title="官方储备资产（2026年）">'
        "2026</a>"
        '<a href="/safe/2018/0112/8051.html" title="官方储备资产（2015年6月-2017年12月）">'
        "历史</a>"
    )

    assert service.parse_yearly_links(html) == [
        (2025, "/safe/2025/0206/25745.html"),
        (2026, "/safe/2026/0205/27113.html"),
    ]


def test_文章页候选表格链接去重保序():
    html = (
        '<a href="/safe/2019/0211/11348.html"></a>'
        '<a href="/safe/2018/0408/8785.html"></a>'
        '<a href="/safe/2018/0408/8785.html"></a>'
        '<a href="/safe/index.html"></a>'
    )

    assert service.parse_table_links(html) == [
        "/safe/2019/0211/11348.html",
        "/safe/2018/0408/8785.html",
    ]


def test_按年份挑选正确的表格页(monkeypatch):
    pages = {
        "/article": (
            '<a href="/safe/2018/0408/8785.html"></a>'
            '<a href="/safe/2019/0211/11348.html"></a>'
        ),
        "/safe/2018/0408/8785.html": table_html(
            2018, list(range(1, 13)), [5924] * 12
        ),
        "/safe/2019/0211/11348.html": table_html(
            2019, list(range(1, 13)), [6000] * 12
        ),
    }
    monkeypatch.setattr(
        service, "_fetch_text", lambda path, symbol=None: pages[path]
    )

    rows = service._fetch_year(2019, "/article")

    assert rows["2019-01"] == 6000
    assert "2018-01" not in rows


def test_payload折算吨并计算环比():
    rows = {"2025-01": 6900.0, "2025-02": 7000.0}

    payload = service.build_payload(rows)

    assert payload["group"] == "goldReserves"
    assert payload["dates"] == ["2025-01", "2025-02"]
    assert payload["as_of"] == "2025-02"
    assert payload["series"]["holdings"] == [2146.14, 2177.24]
    metric = payload["metrics"][0]
    assert metric["latest"] == 2177.24
    assert metric["latest_date"] == "2025-02"
    assert metric["prior"] == 2146.14
    assert metric["delta"] == 31.1
    assert round(metric["delta_pct"], 2) == 1.45
    assert payload["coverage"] == {
        "trading_days": 2,
        "target": 2,
        "complete": True,
    }
    assert payload["sources"][0]["start"] == "2025-01"
    assert payload["sources"][0]["latest"] == "2025-02"


def test_payload跨年环比与缺失上月():
    rows = {"2025-12": 7415.0, "2026-01": 7419.0}

    metric = service.build_payload(rows)["metrics"][0]

    assert metric["latest_date"] == "2026-01"
    assert metric["prior"] is not None
    assert metric["delta"] == round(7419 * service._WAN_OUNCE_TO_TONNE, 2) - round(
        7415 * service._WAN_OUNCE_TO_TONNE, 2
    )

    single = service.build_payload({"2026-03": 7438.0})
    assert single["metrics"][0]["delta"] is None
    assert single["metrics"][0]["delta_pct"] is None


def test_空数据payload不生成指标():
    payload = service.build_payload({})

    assert payload["dates"] == []
    assert payload["metrics"] == []
    assert payload["as_of"] is None
    assert payload["coverage"]["complete"] is True


def test_曲线接口转发黄金储备分组(monkeypatch):
    calls = []

    def fake_payload(group_id, days, *, force=False):
        calls.append((group_id, days, force))
        return {"group": group_id, "dates": [], "metrics": [], "series": {}}

    monkeypatch.setattr(service, "get_curve_payload", fake_payload)
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: None

    response = TestClient(app).get(
        "/api/etf-shares/curve?group=goldReserves&days=90"
    )
    refresh = TestClient(app).post(
        "/api/etf-shares/refresh?group=goldReserves&days=90"
    )

    assert response.status_code == 200
    assert refresh.status_code == 200
    assert calls == [
        ("goldReserves", 90, False),
        ("goldReserves", 90, True),
    ]


def _new_store():
    return StatisticsStore(
        namespace=service.GOLD_RESERVES_GROUP_ID, default_ttl_sec=service._CACHE_TTL_S
    )


def test_写入后新实例读回且每次读取数据库(statistics_db, monkeypatch):
    rows = {"2026-09": 7419.0}
    service._store.set("index", {"2026": "/2026"})
    fetch = Mock(return_value=rows)
    monkeypatch.setattr(service, "_fetch_year", fetch)
    assert service.load_rows() == rows
    restarted = _new_store()
    assert restarted.get("year:2026") == rows
    monkeypatch.setattr(service, "_store", restarted)
    fetch.reset_mock()
    loaded = service.load_rows()
    loaded["2026-09"] = -1
    assert service.load_rows() == rows
    _new_store().set("year:2026", {"2026-09": 7420.0})
    assert service.load_rows() == {"2026-09": 7420.0}
    fetch.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
def test_到期或force合并缩窗响应且同月不重复(statistics_db, monkeypatch, force):
    service._store.set("index", {"2026": "/2026"})
    previous = {"2026-01": 7410.0, "2026-09": 7419.0, "2026-10": 7420.0}
    incoming = {"2026-08": 7415.0, "2026-09": 7421.0, "2026-10": None}
    service._store.set("year:2026", previous, ttl_sec=service._CACHE_TTL_S if force else -1)
    fetch = Mock(return_value=incoming)
    monkeypatch.setattr(service, "_fetch_year", fetch)
    monkeypatch.setattr(service, "_fetch_text", lambda *args, **kw: "")
    expected = {"2026-01": 7410.0, "2026-08": 7415.0, "2026-09": 7421.0, "2026-10": 7420.0}
    assert service.load_rows(force=force) == expected
    assert service.load_rows(force=force) == expected
    assert _new_store().get("year:2026") == expected
    assert fetch.call_count == (2 if force else 1)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("has_history", [False, True])
@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_索引与年度异常回退历史且不续期(statistics_db, monkeypatch, force, has_history, failure):
    previous = {"2025-01": 6900.0} if has_history else {}
    if has_history:
        service._store.set("index", {"2025": "/2025"}, ttl_sec=-1)
        service._store.set("year:2025", previous, ttl_sec=-1)
    network = Mock(return_value=None)
    if failure == "exception":
        network.side_effect = httpx.ConnectError("SAFE unavailable")
    monkeypatch.setattr(service, "market_get", network)
    write = Mock(wraps=service._store.set)
    monkeypatch.setattr(service._store, "set", write)
    assert service.load_rows(force=force) == previous
    assert _new_store().get("year:2025", allow_stale=True) == (previous if has_history else None)
    assert _new_store().get("year:2025") is None
    write.assert_not_called()
    assert network.called


@pytest.mark.parametrize("failed_year", [2025, 2026])
def test_单年失败不随其他年续期且下次仅重试失败年(statistics_db, monkeypatch, failed_year):
    service._store.set("index", {"2025": "/2025", "2026": "/2026"})
    for year in (2025, 2026):
        service._store.set(f"year:{year}", {f"{year}-01": 7000.0}, ttl_sec=-1)

    def fetch_year(year, path):
        if year == failed_year:
            raise httpx.ReadTimeout("year unavailable")
        return {f"{year}-01": 7400.0}

    fetch = Mock(side_effect=fetch_year)
    monkeypatch.setattr(service, "_fetch_year", fetch)
    result = service.load_rows()
    assert result[f"{failed_year}-01"] == 7000.0
    assert _new_store().get(f"year:{failed_year}") is None
    fetch.reset_mock()
    fetch.side_effect = lambda year, path: {f"{year}-01": 7500.0}
    assert service.load_rows()[f"{failed_year}-01"] == 7500.0
    fetch.assert_called_once_with(failed_year, f"/{failed_year}")


def test_索引更新仍保留已存旧年度(monkeypatch):
    service._store.set("index", {"2025": "/old"}, ttl_sec=-1)
    monkeypatch.setattr(service, "_fetch_text", lambda *args, **kw: '<a href="/safe/2026/0205/27113.html" title="官方储备资产（2026年）"></a>')
    assert set(service._load_index(force=True)) == {"2025", "2026"}


def test_数据库写入异常不被网络回退吞掉(statistics_db, monkeypatch):
    service._store.set("index", {"2026": "/2026"})
    monkeypatch.setattr(service, "_fetch_year", lambda *args: {"2026-09": 7419.0})
    monkeypatch.setattr(service._store, "set", Mock(side_effect=SQLAlchemyError("write failed")))
    with pytest.raises(SQLAlchemyError, match="write failed"):
        service.load_rows()
