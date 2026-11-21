"""生猪周期：农业农村部月度表解析、中国养猪网日度解析、payload 与接口。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.market import hog_cycle_service as service
from src.modules.market.api import etf_shares as etf_shares_api
from src.platform.persistence.database import get_db


def table_row(index: int, label: str, value: str, mom: str = "0.1%") -> str:
    return (
        f"<tr><td>{index}</td><td>{label}</td><td>{value}</td>"
        f"<td>{mom}</td><td>0.1%</td></tr>"
    )


def monthly_page() -> str:
    rows = [
        table_row(
            1,
            "2026年3季度末能繁母猪存栏（万头）",
            "3602<br><span>（相当于调控目标的96.0%）</span>",
            "-4.7%",
        ),
        table_row(2, "2026年3季度末生猪存栏（万头）", "42533"),
        table_row(3, "2026年前3季度生猪出栏（万头）", "53739", "—"),
        table_row(4, "2026年9月份生猪定点屠宰企业屠宰量（万头）", "3986", "6.2%"),
        table_row(5, "2026年1-9月生猪定点屠宰企业屠宰量（万头）", "34075", "—"),
        table_row(6, "2026年前3季度猪肉产量（万吨）", "4508", "—"),
        table_row(7, "2026年9月份全国二元母猪销售价格（元/公斤）", "26.57"),
        table_row(8, "2026年9月份全国仔猪价格（元/公斤）", "20.51"),
        table_row(9, "2026年9月份全国生猪出场价格（元/公斤）", "11.05"),
        table_row(10, "2026年9月份全国批发市场白条猪价格（元/公斤）", "16.33"),
        table_row(11, "2026年9月份36个大中城市批发市场白条猪价格（元/公斤）", "17.34"),
        table_row(12, "2026年9月份全国生猪出场价格（元/公斤）", "—"),
    ]
    return "<html><body><table><tbody>" + "".join(rows) + "</tbody></table></body></html>"


def test_月度表解析季度与月度指标():
    rows = service.parse_month_page(monthly_page())

    assert rows == {
        "2026-09": {
            "sow": 3602,
            "hog_stock": 42533,
            "slaughter": 3986,
            "bboar_price": 26.57,
            "piglet_price": 20.51,
            "hog_price": 11.05,
            "pork_wholesale": 16.33,
        }
    }


def test_月度表忽略累计值与空值():
    rows = service.parse_month_page(monthly_page())["2026-09"]

    assert "hog_slaughter" not in rows
    assert "pork_output" not in rows
    assert rows["hog_price"] == 11.05
    assert service.parse_month_page("") == {}
    assert service.parse_month_page(None) == {}


def test_日度接口解析():
    payload = {
        "code": 200,
        "data": [
            {"pigprice": "9.23", "pricedate": "2026-11-17"},
            {"pigprice": "9.32", "pricedate": "2026-11-18"},
            {"pigprice": "", "pricedate": "2026-11-19"},
        ],
    }

    assert service.parse_daily(payload, "pigprice") == {
        "2026-11-17": 9.23,
        "2026-11-18": 9.32,
    }
    assert service.parse_daily(None, "pigprice") == {}
    assert service.parse_daily({"data": "bad"}, "pigprice") == {}


def test_猪粮比预警分级():
    assert service.ratio_level(4.5) == "一级预警"
    assert service.ratio_level(5.5) == "二级预警"
    assert service.ratio_level(7.2) == "正常"
    assert service.ratio_level(9.5) == "三级预警"
    assert service.ratio_level(10.5) == "二级预警"
    assert service.ratio_level(12.5) == "一级预警"
    assert service.ratio_level(None) is None


def test_payload序列指标与政策区间():
    months = {
        "2026-06": {
            "sow": 3780,
            "hog_stock": 43000,
            "hog_price": 11.01,
            "piglet_price": 21.91,
            "bboar_price": 26.68,
            "slaughter": 3755,
        },
        "2026-08": {
            "hog_price": 11.09,
            "piglet_price": 21.0,
            "bboar_price": 26.6,
            "slaughter": 3800,
        },
        "2026-09": {
            "sow": 3602,
            "hog_stock": 42533,
            "hog_price": 11.05,
            "piglet_price": 20.51,
            "bboar_price": 26.57,
            "slaughter": 3986,
        },
    }
    daily = {
        "hog_price": {"2026-11-17": 9.23, "2026-11-18": 9.32},
        "corn": {"2026-11-17": 2313, "2026-11-18": 2334},
    }

    payload = service.build_payload(months, daily)

    assert payload["group"] == "hogCycle"
    assert payload["group_label"] == "生猪周期"
    assert payload["dates"] == ["2026-06-30", "2026-08-31", "2026-09-30"]
    assert payload["series"]["hog_price"] == [11.01, 11.09, 11.05]
    assert payload["quarterly"] == {
        "dates": ["2026-06-30", "2026-09-30"],
        "sow": [3780, 3602],
        "hog_stock": [43000, 42533],
    }
    assert payload["daily"]["dates"] == ["2026-11-17", "2026-11-18"]
    assert payload["daily"]["ratio"] == [3.99, 3.993]
    assert payload["policy"] == {
        "normal": 3750,
        "green_low": 3450,
        "green_high": 3863,
    }
    metrics = {item["id"]: item for item in payload["metrics"]}
    assert metrics["sow"]["latest"] == 3602
    assert metrics["sow"]["prior"] == 3780
    assert metrics["sow"]["delta"] == -178
    assert metrics["sow"]["delta_pct"] == -4.709
    assert metrics["hog_price"]["delta"] == -0.04
    assert metrics["corn_hog_ratio"]["latest"] == 3.993
    assert metrics["corn_hog_ratio"]["delta"] == 0.003
    assert metrics["slaughter"]["latest"] == 3986
    assert payload["as_of"] == "2026-11-18"
    assert payload["coverage"] == {
        "trading_days": 3,
        "target": 3,
        "complete": True,
    }
    assert [source["id"] for source in payload["sources"]] == ["moa", "zhujia"]
    assert payload["sources"][0]["latest"] == "2026-09"
    assert payload["sources"][1]["latest"] == "2026-11-18"


def test_空数据不生成指标():
    payload = service.build_payload({}, {})

    assert payload["dates"] == []
    assert payload["metrics"] == []
    assert payload["quarterly"] == {"dates": [], "sow": [], "hog_stock": []}
    assert payload["daily"] == {
        "dates": [],
        "ratio": [],
        "hog_price": [],
        "corn": [],
    }
    assert payload["as_of"] is None
    assert payload["sources"][0]["start"] is None


def test_曲线接口转发生猪分组(monkeypatch):
    calls = []

    def fake_payload(group_id, days, *, force=False):
        calls.append((group_id, days, force))
        return {"group": group_id, "dates": [], "metrics": [], "series": {}}

    monkeypatch.setattr(service, "get_curve_payload", fake_payload)
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")
    app.dependency_overrides[get_db] = lambda: None

    response = TestClient(app).get("/api/etf-shares/curve?group=hogCycle&days=90")
    refresh = TestClient(app).post(
        "/api/etf-shares/refresh?group=hogCycle&days=90"
    )

    assert response.status_code == 200
    assert refresh.status_code == 200
    assert calls == [("hogCycle", 90, False), ("hogCycle", 90, True)]
