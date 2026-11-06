"""集运运价 SCFI：官方最新一期解析、周度历史解析、payload 与接口。"""

from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.market import scfi_service as service
from src.modules.market.api import etf_shares as etf_shares_api
from src.platform.persistence.database import get_db


def official_payload(
    current_date: str = "2026-10-30",
    last_date: str = "2026-10-23",
    current: float | None = 3687.3527,
    last: float | None = 3681.3369,
) -> dict:
    return {
        "data": {
            "currentDate": current_date,
            "lastDate": last_date,
            "lineDataList": [
                {
                    "properties": {"lineName_EN": "Comprehensive Index"},
                    "currentContent": None,
                    "lastContent": None,
                    "dataItemTypeName": "SCFI_L1",
                },
                {
                    "properties": {"lineName_EN": "Comprehensive Index"},
                    "currentContent": current,
                    "lastContent": last,
                    "dataItemTypeName": "SCFI_T",
                },
            ],
        }
    }


def mirror_html(entries: list[tuple[str, float, float]]) -> str:
    blocks = []
    for day, previous, current in entries:
        blocks.append(
            f"<div><h3>{day}</h3><p>\U0001f4c8 {previous}→{current}"
            f"(+{current - previous:.2f}), up 0.1% from the previous week</p></div>"
        )
    return "<html><body>" + "".join(blocks) + "</body></html>"


def test_官方接口解析最新两期():
    rows = service.parse_official(official_payload())

    assert rows == {"2026-10-23": 3681.3369, "2026-10-30": 3687.3527}


def test_官方接口忽略缺失数值():
    payload = official_payload(current=None)

    assert service.parse_official(payload) == {"2026-10-23": 3681.3369}
    assert service.parse_official(None) == {}
    assert service.parse_official({"data": {"lineDataList": []}}) == {}


def test_周度历史解析日期与数值():
    html = mirror_html(
        [
            ("2026/10/30", 3681.34, 3687.35),
            ("2026/10/23", 3679.58, 3681.34),
            ("2026/10/16", 3664.11, 3679.58),
        ]
    )

    rows = service.parse_weekly_rows(html)

    assert rows == {
        "2026-10-30": 3687.35,
        "2026-10-23": 3681.34,
        "2026-10-16": 3679.58,
    }


def test_周度历史解析兼容SCFI前缀与千分位():
    html = (
        "<p>2025/06/13</p><p>SCFI 2,240.35→2,088.24(-152.10), down 6.8%</p>"
        "<p>2025/06/06</p><p>SCFI 2,072.71→2,240.35(+167.64), up 8.1%</p>"
    )

    assert service.parse_weekly_rows(html) == {
        "2025-06-13": 2088.24,
        "2025-06-06": 2240.35,
    }
    assert service.parse_weekly_rows(None) == {}
    assert service.parse_weekly_rows("<p>no data</p>") == {}


def test_快照合并时官方数值优先(monkeypatch):
    monkeypatch.setattr(
        service,
        "_fetch_mirror",
        lambda: {"2026-10-23": 3681.34, "2026-10-16": 3679.58},
    )
    monkeypatch.setattr(
        service,
        "_fetch_official",
        lambda: {"2026-10-23": 3681.3369, "2026-10-30": 3687.3527},
    )

    snapshot = service._load_snapshot(force=True)

    assert snapshot["rows"] == {
        "2026-10-16": 3679.58,
        "2026-10-23": 3681.3369,
        "2026-10-30": 3687.3527,
    }
    assert snapshot["mirror"]["2026-10-23"] == 3681.34
    assert snapshot["official"]["2026-10-30"] == 3687.3527


def test_单侧抓取失败仍可使用另一侧(monkeypatch):
    def fail():
        raise RuntimeError("network down")

    monkeypatch.setattr(service, "_fetch_mirror", fail)
    monkeypatch.setattr(
        service,
        "_fetch_official",
        lambda: {"2026-10-23": 3681.34, "2026-10-30": 3687.35},
    )

    snapshot = service._load_snapshot(force=True)

    assert snapshot["mirror"] == {}
    assert snapshot["rows"]["2026-10-30"] == 3687.35


def test_payload计算周环比与来源():
    rows = {"2026-10-23": 3681.34, "2026-10-30": 3687.3527}
    official = {"2026-10-23": 3681.3369, "2026-10-30": 3687.3527}
    mirror = {"2026-10-16": 3679.58, "2026-10-23": 3681.34}

    payload = service.build_payload(rows, mirror=mirror, official=official)

    assert payload["group"] == "scfi"
    assert payload["dates"] == ["2026-10-23", "2026-10-30"]
    assert payload["as_of"] == "2026-10-30"
    assert payload["series"]["scfi"] == [3681.34, 3687.35]
    metric = payload["metrics"][0]
    assert metric["id"] == "scfi"
    assert metric["latest"] == 3687.35
    assert metric["latest_date"] == "2026-10-30"
    assert metric["prior"] == 3681.34
    assert metric["delta"] == 6.01
    assert round(metric["delta_pct"], 2) == 0.16
    assert payload["coverage"] == {
        "trading_days": 2,
        "target": 2,
        "complete": True,
    }
    assert [source["id"] for source in payload["sources"]] == ["sse", "opencontainer"]
    assert payload["sources"][0]["start"] == "2026-10-23"
    assert payload["sources"][0]["latest"] == "2026-10-30"
    assert payload["sources"][1]["start"] == "2026-10-16"
    assert payload["sources"][1]["latest"] == "2026-10-23"


def test_仅官方数据时来源只有交易所():
    payload = service.build_payload(
        {"2026-10-30": 3687.35},
        official={"2026-10-30": 3687.35},
    )

    assert [source["id"] for source in payload["sources"]] == ["sse"]
    assert payload["metrics"][0]["delta"] is None
    assert payload["metrics"][0]["delta_pct"] is None


def test_空数据payload不生成指标():
    payload = service.build_payload({})

    assert payload["dates"] == []
    assert payload["metrics"] == []
    assert payload["as_of"] is None
    assert payload["coverage"]["complete"] is True
    assert payload["sources"][0]["start"] is None
    assert payload["sources"][0]["latest"] is None


def test_曲线接口转发集运分组(monkeypatch):
    calls = []

    def fake_payload(group_id, days, *, force=False):
        calls.append((group_id, days, force))
        return {"group": group_id, "dates": [], "metrics": [], "series": {}}

    monkeypatch.setattr(service, "get_curve_payload", fake_payload)
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: None

    response = TestClient(app).get("/api/etf-shares/curve?group=scfi&days=90")
    refresh = TestClient(app).post("/api/etf-shares/refresh?group=scfi&days=90")

    assert response.status_code == 200
    assert refresh.status_code == 200
    assert calls == [("scfi", 90, False), ("scfi", 90, True)]


def test_分源快照持久化后新Store实例不重复抓取(monkeypatch, statistics_db):
    mirror = {"2026-10-16": 3679.58, "2026-10-23": 3681.34}
    official = {"2026-10-23": 3681.3369, "2026-10-30": 3687.3527}
    fetch_mirror = Mock(return_value=mirror)
    fetch_official = Mock(return_value=official)
    monkeypatch.setattr(service, "_fetch_mirror", fetch_mirror)
    monkeypatch.setattr(service, "_fetch_official", fetch_official)

    expected = service._load_snapshot(force=True)
    store = service.StatisticsStore(
        namespace=service.SCFI_GROUP_ID, default_ttl_sec=service._CACHE_TTL_S
    )
    assert store.get("mirror") == mirror
    assert store.get("official") == official
    monkeypatch.setattr(service, "_store", store)
    fetch_mirror.reset_mock()
    fetch_official.reset_mock()

    assert service._load_snapshot() == expected
    fetch_mirror.assert_not_called()
    fetch_official.assert_not_called()


def test_强制更新同周数值并保留两源历史(monkeypatch, statistics_db):
    service._store.set("mirror", {"2026-10-16": 3679.58, "2026-10-23": 3681.34})
    service._store.set("official", {"2026-10-23": 3681.3369, "2026-10-30": 3687.3527})
    fetch_mirror = Mock(return_value={"2026-10-30": 3690.12, "2026-11-06": 3700})
    fetch_official = Mock(return_value={"2026-10-30": 3690.1234})
    monkeypatch.setattr(service, "_fetch_mirror", fetch_mirror)
    monkeypatch.setattr(service, "_fetch_official", fetch_official)

    snapshot = service._load_snapshot(force=True)

    assert snapshot["rows"] == {
        "2026-10-16": 3679.58,
        "2026-10-23": 3681.3369,
        "2026-10-30": 3690.1234,
        "2026-11-06": 3700,
    }
    assert service._store.get("mirror") == snapshot["mirror"]
    assert service._store.get("official") == snapshot["official"]
    assert snapshot["official"]["2026-10-23"] == 3681.3369
    fetch_mirror.assert_called_once()
    fetch_official.assert_called_once()


@pytest.mark.parametrize("failed_key", ["mirror", "official"])
@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_单源失败独立回退且保留官方优先级与来源(
    monkeypatch, statistics_db, caplog, failed_key, failure
):
    previous = {
        "mirror": {"2026-10-16": 1000, "2026-10-23": 1100},
        "official": {"2026-10-23": 1100.1234, "2026-10-30": 1200.1234},
    }
    incoming = {
        "mirror": {"2026-10-23": 1101, "2026-10-30": 1201, "2026-11-06": 1300},
        "official": {"2026-10-30": 1201.5678, "2026-11-06": 1300.5678},
    }
    fetches = {}
    for key in ("mirror", "official"):
        service._store.set(key, previous[key], ttl_sec=-1)
        fetch = Mock(return_value={} if key == failed_key else incoming[key])
        if key == failed_key and failure == "exception":
            fetch.side_effect = RuntimeError("network down")
        fetches[key] = fetch
        monkeypatch.setattr(service, f"_fetch_{key}", fetch)

    snapshot = service._load_snapshot()

    expected = {
        key: previous[key] if key == failed_key else {**previous[key], **incoming[key]}
        for key in previous
    }
    assert snapshot["mirror"] == expected["mirror"]
    assert snapshot["official"] == expected["official"]
    assert snapshot["rows"] == {**expected["mirror"], **expected["official"]}
    payload = service.build_payload(
        snapshot["rows"], mirror=snapshot["mirror"], official=snapshot["official"]
    )
    sources = {item["id"]: item for item in payload["sources"]}
    for key, source_id in (("mirror", "opencontainer"), ("official", "sse")):
        assert sources[source_id]["start"] == min(expected[key])
        assert sources[source_id]["latest"] == max(expected[key])
        assert service._store.get(key, allow_stale=True) == expected[key]
        assert service._store.get(key) == (None if key == failed_key else expected[key])
        fetches[key].assert_called_once()

    assert service._load_snapshot() == snapshot
    assert fetches[failed_key].call_count == 2
    assert fetches["mirror" if failed_key == "official" else "official"].call_count == 1
    if failure == "exception":
        assert "network down" in caplog.text
        assert any(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_两源空响应或异常不清除历史(monkeypatch, statistics_db, force, failure):
    previous = {
        "mirror": {"2026-10-30": 3687.35},
        "official": {"2026-10-30": 3687.3527},
    }
    for key in previous:
        service._store.set(key, previous[key], ttl_sec=-1)
        fetch = Mock(return_value={})
        if failure == "exception":
            fetch.side_effect = RuntimeError("network down")
        monkeypatch.setattr(service, f"_fetch_{key}", fetch)

    snapshot = service._load_snapshot(force=force)

    assert snapshot == {"rows": previous["official"], **previous}
    for key in previous:
        assert service._store.get(key) is None
        assert service._store.get(key, allow_stale=True) == previous[key]


@pytest.mark.parametrize("operation", ["get", "set"])
def test_数据库读写异常向上传播(monkeypatch, statistics_db, operation):
    monkeypatch.setattr(service, "_fetch_mirror", Mock(return_value={"2026-10-30": 3687.35}))
    monkeypatch.setattr(service, "_fetch_official", Mock(return_value={"2026-10-30": 3687.3527}))
    monkeypatch.setattr(
        service._store, operation, Mock(side_effect=RuntimeError("sqlite unavailable"))
    )

    with pytest.raises(RuntimeError, match="sqlite unavailable"):
        service._load_snapshot(force=True)
