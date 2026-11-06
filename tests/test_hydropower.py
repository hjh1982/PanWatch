"""长电来水发电：公告筛选、正文解析、单季差分、payload 与接口。"""

from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.market import hydropower_service as service
from src.modules.market.api import etf_shares as etf_shares_api
from src.platform.persistence.database import get_db


def announcement_item(title: str, art_code: str, notice_date: str = "2026-10-15"):
    return {
        "title": title,
        "art_code": art_code,
        "notice_date": notice_date,
    }


def announcement_list(*items: dict) -> dict:
    return {"data": {"list": list(items)}}


def q1_text() -> str:
    return (
        "长江电力2023年第一季度发电量完成情况公告。"
        "乌东德水库来水总量约100亿立方米，三峡水库来水总量约300亿立方米。"
        "第一季度总发电量约500亿千瓦时。"
        "乌东德电站完成发电量60亿千瓦时，白鹤滩电站完成发电量80亿千瓦时，"
        "溪洛渡电站完成发电量100亿千瓦时，向家坝电站完成发电量60亿千瓦时，"
        "三峡电站完成发电量150亿千瓦时，葛洲坝电站完成发电量50亿千瓦时。"
    )


def q2_text() -> str:
    return (
        "长江电力2023年第二季度发电量完成情况公告。"
        "乌东德水库来水总量约300亿立方米，三峡水库来水总量约800亿立方米。"
        "第二季度总发电量约700亿千瓦时。"
        "乌东德电站完成发电量90亿千瓦时，白鹤滩电站完成发电量110亿千瓦时，"
        "溪洛渡电站完成发电量150亿千瓦时，向家坝电站完成发电量90亿千瓦时，"
        "三峡电站完成发电量190亿千瓦时，葛洲坝电站完成发电量70亿千瓦时。"
    )


def q3_text() -> str:
    return (
        "长江电力2023年第三季度发电量完成情况公告。"
        "乌东德水库来水总量约500亿立方米，三峡水库来水总量约1300亿立方米。"
        "第三季度总发电量约800亿千瓦时。"
        "乌东德电站完成发电量100亿千瓦时，白鹤滩电站完成发电量130亿千瓦时，"
        "溪洛渡电站完成发电量180亿千瓦时，向家坝电站完成发电量100亿千瓦时，"
        "三峡电站完成发电量210亿千瓦时，葛洲坝电站完成发电量80亿千瓦时。"
    )


def annual_single_column_text() -> str:
    """2023 年年度公告只有一列年度值，需要在季度序列里差分出 Q4。"""
    return (
        "长江电力2023年发电量完成情况公告。"
        "乌东德水库来水总量约600亿立方米，三峡水库来水总量约1500亿立方米。"
        "总发电量约2,060亿千瓦时。"
        "乌东德电站完成发电量260亿千瓦时，白鹤滩电站完成发电量330亿千瓦时，"
        "溪洛渡电站完成发电量440亿千瓦时，向家坝电站完成发电量260亿千瓦时，"
        "三峡电站完成发电量560亿千瓦时，葛洲坝电站完成发电量210亿千瓦时。"
    )


def annual_two_column_text() -> str:
    """2024 年起的年度公告同时列出第四季度与全年，首列即 Q4。"""
    return (
        "长江电力2024年发电量完成情况公告。"
        "乌东德水库来水总量约700亿立方米，三峡水库来水总量约1800亿立方米。"
        "第四季度总发电量约60亿千瓦时。"
        "乌东德电站第四季度完成发电量10亿千瓦时，白鹤滩电站第四季度完成发电量11亿千瓦时，"
        "溪洛渡电站第四季度完成发电量12亿千瓦时，向家坝电站第四季度完成发电量13亿千瓦时，"
        "三峡电站第四季度完成发电量14亿千瓦时，葛洲坝电站第四季度完成发电量15亿千瓦时。"
    )


def test_公告标题识别季度():
    assert service.period_from_title("长江电力2026年第一季度发电量完成情况公告") == "2026Q1"
    assert service.period_from_title("长江电力2026年半年度发电量完成情况公告") == "2026Q2"
    assert service.period_from_title("长江电力2026年第三季度发电量完成情况公告") == "2026Q3"
    assert service.period_from_title("长江电力2026年发电量完成情况公告") == "2026Q4"
    assert service.period_from_title("长江电力2026年1-2月发电量完成情况公告") is None
    assert service.period_from_title("长江电力2026年第一季度业绩说明会") is None
    assert service.period_from_title("长江电力发电量完成情况公告") is None


def test_公告列表去重排序并过滤无关公告():
    payload = announcement_list(
        announcement_item("长江电力2025年发电量完成情况公告", "annual-2025"),
        announcement_item("长江电力2026年第三季度发电量完成情况公告", "q3-2026"),
        announcement_item("长江电力2026年第三季度发电量完成情况公告", "q3-2026-dup"),
        announcement_item("长江电力2026年第一季度发电量完成情况公告", "q1-2026"),
        announcement_item("长江电力2022年发电量完成情况公告", "annual-2022"),
        announcement_item("长江电力2026年1-2月发电量完成情况公告", "jan-feb-2026"),
        announcement_item("长江电力2026年第三季度业绩说明会", "roadshow-2026"),
    )

    items = service.parse_announcement_list(payload)

    assert [item["period"] for item in items] == ["2025Q4", "2026Q1", "2026Q3"]
    assert items[2]["art_code"] == "q3-2026"
    assert items[2]["notice_date"] == "2026-10-15"


def test_解析季度公告的累计来水与当季发电量():
    record = service.parse_announcement(q3_text(), "2023Q3")

    assert record["total"] == 800
    assert record["cumulative_total"] == 800
    assert record["cumulative_udongde"] == 500
    assert record["cumulative_three_gorges"] == 1300
    assert record["has_quarter_table"] is False
    assert record["stations"] == {
        "udongde": 100,
        "baihetan": 130,
        "xiluodu": 180,
        "xiangjiaba": 100,
        "three_gorges": 210,
        "gezhouba": 80,
    }


def test_解析单列年度公告():
    record = service.parse_announcement(annual_single_column_text(), "2023Q4")

    assert record["cumulative_total"] == 2060
    assert record["total"] == 2060
    assert record["has_quarter_table"] is False
    assert record["stations"]["three_gorges"] == 560


def test_解析两列年度公告():
    record = service.parse_announcement(annual_two_column_text(), "2024Q4")

    assert record["total"] == 60
    assert record["has_quarter_table"] is True
    assert record["stations"] == {
        "udongde": 10,
        "baihetan": 11,
        "xiluodu": 12,
        "xiangjiaba": 13,
        "three_gorges": 14,
        "gezhouba": 15,
    }


def test_构建单季数据差分来水并还原第四季度():
    announcements = [
        service.parse_announcement(q1_text(), "2023Q1"),
        service.parse_announcement(q2_text(), "2023Q2"),
        service.parse_announcement(q3_text(), "2023Q3"),
        service.parse_announcement(annual_single_column_text(), "2023Q4"),
    ]

    rows = service.build_rows(announcements)

    assert [row["period"] for row in rows] == ["2023Q1", "2023Q2", "2023Q3", "2023Q4"]
    assert [row["generation"] for row in rows] == [500, 700, 800, 60]
    assert [row["inflow_udongde"] for row in rows] == [100, 200, 200, 100]
    assert [row["inflow_three_gorges"] for row in rows] == [300, 500, 500, 200]
    assert [row["stations"]["udongde"] for row in rows] == [60, 90, 100, 10]
    assert [row["stations"]["three_gorges"] for row in rows] == [150, 190, 210, 10]


def test_构建单季数据使用两列公告的第四季度列():
    announcements = [
        service.parse_announcement(q1_text(), "2023Q1"),
        service.parse_announcement(q2_text(), "2023Q2"),
        service.parse_announcement(q3_text(), "2023Q3"),
        service.parse_announcement(annual_two_column_text(), "2023Q4"),
    ]

    rows = service.build_rows(announcements)

    assert rows[-1]["period"] == "2023Q4"
    assert rows[-1]["generation"] == 60
    assert rows[-1]["stations"]["gezhouba"] == 15
    assert rows[-1]["inflow_udongde"] == 200


def test_无法识别的正文返回空():
    assert service.parse_announcement("", "2026Q3") is None
    assert service.parse_announcement("长江电力公告正文", "2026Q3") is None
    assert service.parse_announcement(None, "2026Q3") is None


def test_payload指标序列与来源():
    stations = {
        "udongde": 1,
        "baihetan": 2,
        "xiluodu": 3,
        "xiangjiaba": 4,
        "three_gorges": 5,
        "gezhouba": 6,
    }
    rows = [
        {
            "period": "2025Q4",
            "generation": 1000,
            "inflow_udongde": 400,
            "inflow_three_gorges": 1200,
            "stations": stations,
        },
        {
            "period": "2026Q1",
            "generation": 923.14,
            "inflow_udongde": 425.4,
            "inflow_three_gorges": 1273.17,
            "stations": stations,
        },
    ]

    payload = service.build_payload(rows)

    assert payload["group"] == "hydropower"
    assert payload["group_label"] == "长电来水发电"
    assert payload["dates"] == ["2025Q4", "2026Q1"]
    assert payload["as_of"] == "2026Q1"
    assert [metric["id"] for metric in payload["metrics"]] == [
        "generation",
        "inflow_udongde",
        "inflow_three_gorges",
    ]
    generation = payload["metrics"][0]
    assert generation["latest"] == 923.14
    assert generation["prior"] == 1000
    assert generation["delta"] == -76.86
    assert generation["delta_pct"] == -7.686
    assert payload["series"]["generation"] == [1000, 923.14]
    assert payload["series"]["inflow_three_gorges"] == [1200, 1273.17]
    assert payload["series"]["gezhouba"] == [6, 6]
    assert payload["coverage"] == {
        "trading_days": 2,
        "target": 2,
        "complete": True,
    }
    assert payload["sources"][0]["id"] == "eastmoney"
    assert payload["sources"][0]["start"] == "2025Q4"
    assert payload["sources"][0]["latest"] == "2026Q1"


def test_空数据payload不生成指标():
    payload = service.build_payload([])

    assert payload["dates"] == []
    assert payload["metrics"] == []
    assert payload["as_of"] is None
    assert payload["series"]["generation"] == []
    assert payload["sources"][0]["start"] is None
    assert payload["sources"][0]["latest"] is None


def test_曲线接口转发长电分组(monkeypatch):
    calls = []

    def fake_payload(group_id, days, *, force=False):
        calls.append((group_id, days, force))
        return {"group": group_id, "dates": [], "metrics": [], "series": {}}

    monkeypatch.setattr(service, "get_curve_payload", fake_payload)
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: None

    response = TestClient(app).get("/api/etf-shares/curve?group=hydropower&days=90")
    refresh = TestClient(app).post(
        "/api/etf-shares/refresh?group=hydropower&days=90"
    )

    assert response.status_code == 200
    assert refresh.status_code == 200
    assert calls == [("hydropower", 90, False), ("hydropower", 90, True)]


def annual_announcements():
    return [
        service.parse_announcement(q1_text(), "2023Q1"),
        service.parse_announcement(q2_text(), "2023Q2"),
        service.parse_announcement(q3_text(), "2023Q3"),
        service.parse_announcement(annual_single_column_text(), "2023Q4"),
    ]


def test_持久化原始公告并由新Store实例重新构建季度(monkeypatch, statistics_db):
    announcements = annual_announcements()
    fetch = Mock(return_value=announcements)
    monkeypatch.setattr(service, "_fetch_announcements", fetch)

    expected = service.load_rows(force=True)
    store = service.StatisticsStore(
        namespace=service.HYDROPOWER_GROUP_ID, default_ttl_sec=service._CACHE_TTL_S
    )
    assert store.get("announcements") == announcements
    assert store.get("announcements")[-1]["cumulative_total"] == 2060
    assert store.get("announcements")[-1]["stations"]["udongde"] == 260
    assert store.get("rows", allow_stale=True) is None
    assert expected[-1]["generation"] == 60
    monkeypatch.setattr(service, "_store", store)
    build = Mock(wraps=service.build_rows)
    monkeypatch.setattr(service, "build_rows", build)
    fetch.reset_mock()

    assert service.load_rows() == expected
    fetch.assert_not_called()
    build.assert_called_once_with(announcements)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("changed_period", ["2023Q1", "2023Q4"])
def test_部分公告更新保留缺失期间与已知字段并重算季度(
    monkeypatch, statistics_db, changed_period, force
):
    announcements = annual_announcements()
    service._store.set("announcements", announcements, ttl_sec=None if force else -1)
    incoming = {
        "period": changed_period,
        "total": 510 if changed_period == "2023Q1" else 2100,
        "cumulative_total": 510 if changed_period == "2023Q1" else 2100,
        "cumulative_udongde": 110 if changed_period == "2023Q1" else 610,
        "cumulative_three_gorges": None,
        "stations": {
            "udongde": 65 if changed_period == "2023Q1" else 270,
            "three_gorges": None,
        },
    }
    fetch = Mock(return_value=[incoming])
    monkeypatch.setattr(service, "_fetch_announcements", fetch)

    rows = service.load_rows(force=force)

    assert [row["period"] for row in rows] == ["2023Q1", "2023Q2", "2023Q3", "2023Q4"]
    if changed_period == "2023Q1":
        assert [row["generation"] for row in rows] == [510, 700, 800, 50]
        assert [row["inflow_udongde"] for row in rows] == [110, 190, 200, 100]
        assert [row["stations"]["udongde"] for row in rows] == [65, 90, 100, 5]
    else:
        assert [row["generation"] for row in rows] == [500, 700, 800, 100]
        assert [row["inflow_udongde"] for row in rows] == [100, 200, 200, 110]
        assert [row["stations"]["udongde"] for row in rows] == [60, 90, 100, 20]
    assert [row["inflow_three_gorges"] for row in rows] == [300, 500, 500, 200]
    assert [row["stations"]["three_gorges"] for row in rows] == [150, 190, 210, 10]
    saved = {item["period"]: item for item in service._store.get("announcements")}
    for previous in announcements:
        if previous["period"] != changed_period:
            assert saved[previous["period"]] == previous
        else:
            assert (
                saved[changed_period]["cumulative_three_gorges"]
                == previous["cumulative_three_gorges"]
            )
            assert (
                saved[changed_period]["stations"]["three_gorges"]
                == previous["stations"]["three_gorges"]
            )
    assert incoming["cumulative_three_gorges"] is None
    assert incoming["stations"]["three_gorges"] is None
    fetch.assert_called_once()
    assert service.load_rows() == rows
    fetch.assert_called_once()


@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_部分PDF失败仍保存成功公告并使用本地年度重算(monkeypatch, statistics_db, caplog, failure):
    service._store.set("announcements", annual_announcements())
    payload = announcement_list(
        announcement_item("长江电力2023年第一季度发电量完成情况公告", "q1"),
        announcement_item("长江电力2023年第二季度发电量完成情况公告", "q2"),
        announcement_item("长江电力2023年第三季度发电量完成情况公告", "q3"),
        announcement_item("长江电力2023年发电量完成情况公告", "annual"),
    )
    monkeypatch.setattr(service, "_MAX_LIST_PAGES", 1)
    monkeypatch.setattr(service, "market_get", Mock(return_value=payload))

    def fetch_pdf(art_code):
        if art_code == "q1":
            return q1_text().replace("总发电量约500", "总发电量约510")
        if failure == "exception":
            raise RuntimeError("PDF network down")
        return None

    fetch = Mock(side_effect=fetch_pdf)
    monkeypatch.setattr(service, "_fetch_pdf_text", fetch)

    rows = service.load_rows(force=True)

    assert [row["generation"] for row in rows] == [510, 700, 800, 50]
    assert [row["inflow_udongde"] for row in rows] == [100, 200, 200, 100]
    assert rows[-1]["stations"]["udongde"] == 10
    saved = service._store.get("announcements")
    assert len(saved) == 4
    assert saved[0]["total"] == 510
    assert saved[-1]["cumulative_total"] == 2060
    assert fetch.call_count == 4
    if failure == "exception":
        assert "PDF network down" in caplog.text
        assert len([record for record in caplog.records if record.levelname == "WARNING"]) == 3


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_全空或抓取异常不清库也不延长过期时间(
    monkeypatch, statistics_db, caplog, force, failure
):
    announcements = annual_announcements()
    service._store.set("announcements", announcements, ttl_sec=-1)
    fetch = Mock(return_value=[])
    if failure == "exception":
        fetch.side_effect = RuntimeError("network down")
    monkeypatch.setattr(service, "_fetch_announcements", fetch)

    assert service.load_rows(force=force) == service.build_rows(announcements)
    assert service._store.get("announcements") is None
    assert service._store.get("announcements", allow_stale=True) == announcements
    fetch.assert_called_once()
    if failure == "exception":
        assert "network down" in caplog.text
        assert any(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.parametrize("operation", ["get", "set"])
def test_数据库读写异常向上传播(monkeypatch, statistics_db, operation):
    monkeypatch.setattr(service, "_fetch_announcements", Mock(return_value=annual_announcements()))
    monkeypatch.setattr(
        service._store, operation, Mock(side_effect=RuntimeError("sqlite unavailable"))
    )

    with pytest.raises(RuntimeError, match="sqlite unavailable"):
        service.load_rows(force=True)
