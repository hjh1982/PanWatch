"""A 股现金选择权：公告解析、状态判定、事件构建、payload 与接口。"""

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.research import cash_option_service as service
from src.modules.research.api import recommendations


DONGXING_TEXT = (
    "中国国际金融股份有限公司换股吸收合并东兴证券股份有限公司、信达证券股份有限公司。"
    "2026年9月7日本公司A股股票收盘价为14.00元/股，A股异议股东现金选择权行权价格为13.04元/股，"
    "成功申报行使现金选择权的A股股东将以13.04元/股的行权价格获得现金对价。"
    "（二）A股异议股东现金选择权实施股权登记日：2026年9月14日。"
    "（三）申报期间：2026年9月15日至2026年9月17日的9:30-11:30、13:00-15:00。"
    "申报编号：A股申报编号为770007"
)

XINDA_TEXT = (
    "2026年9月14日，公司A股股票收盘价为15.56元/股。"
    "根据公司于2025年度权益分派股权登记日（即2026年7月27日）获取的股东名册，"
    "预计可申报行使现金选择权的A股异议股东持股数量约为不超过11,250.92万股。"
    "（二）A股异议股东现金选择权实施股权登记日：2026年9月14日。"
    "（三）申报期间：2026年9月15日至2026年9月17日的9:30-11:30、13:00-15:00。"
    "（五）现金选择权的申报编号：770008。"
    "（八）行权价格：现金选择权的行权价格为17.75元/股。"
)

KANGJIA_TEXT = (
    "有权行使现金选择权的股东为于现金选择权股权登记日登记在册的全体A股和B股股东。"
    "现金选择权的申报期间为2026年9月30日至2026年10月13日之间的交易日。"
    "（一）现金选择权的代码及简称A股：现金选择权代码：038043现金选择权简称：康佳KJP1"
    "B股：现金选择权代码：238007现金选择权简称：康佳KJP2"
    "现金选择权的行权价格（A股：2.48元/股；B股：0.73港币/股）。"
    "截至2026年9月3日，*ST康佳A、*ST康佳B收盘价分别为2.46元/股、0.71港币/股。"
    "现金选择权股权登记日为2026年9月22日。"
)


def test_解析行权价格申报期与登记日():
    parsed = service.parse_cash_option_text(DONGXING_TEXT)

    assert parsed["exercise_price"] == 13.04
    assert parsed["ref_price"] == 14.0
    assert parsed["window_start"] == "2026-09-15"
    assert parsed["window_end"] == "2026-09-17"
    assert parsed["record_date"] == "2026-09-14"
    assert parsed["apply_code"] == "770007"
    assert parsed["eligibility"] == "dissenting"


def test_股权登记日忽略权益分派语境():
    parsed = service.parse_cash_option_text(XINDA_TEXT)

    assert parsed["record_date"] == "2026-09-14"
    assert parsed["exercise_price"] == 17.75
    assert parsed["apply_code"] == "770008"


def test_深市权利代码与全体股东():
    parsed = service.parse_cash_option_text(KANGJIA_TEXT)

    assert parsed["exercise_price"] == 2.48
    assert parsed["ref_price"] == 2.46
    assert parsed["apply_code"] == "038043"
    assert parsed["eligibility"] == "all"
    assert parsed["record_date"] == "2026-09-22"
    assert parsed["window_start"] == "2026-09-30"
    assert parsed["window_end"] == "2026-10-13"


def test_空文本返回空字段():
    assert service.parse_cash_option_text(None) == {
        "exercise_price": None,
        "ref_price": None,
        "window_start": None,
        "window_end": None,
        "record_date": None,
        "apply_code": None,
        "eligibility": "unknown",
    }


def test_仅识别A股代码():
    assert service.is_a_share("000016")
    assert service.is_a_share("601198")
    assert service.is_a_share("688001")
    assert not service.is_a_share("200016")
    assert not service.is_a_share("900901")
    assert not service.is_a_share("830799")
    assert not service.is_a_share("")


def test_状态边界():
    today = date(2026, 11, 2)

    assert service.classify_status("2026-11-03", "2026-11-20", today) == "upcoming"
    assert service.classify_status("2026-10-20", "2026-11-02", today) == "active"
    assert service.classify_status("2026-10-20", "2026-11-01", today) == "ended"
    assert service.classify_status(None, None, today) == "unknown"


def test_公告优先级过滤():
    assert service._announcement_priority("关于现金选择权实施公告") == 0
    assert service._announcement_priority("关于现金选择权实施的第一次提示性公告") == 1
    assert service._announcement_priority("关于现金选择权行权的公告") == 2
    assert service._announcement_priority("关于现金选择权申报结果公告") is None
    assert service._announcement_priority("关于现金选择权清算交割公告") is None
    assert service._announcement_priority("Cash Option Announcement") is None


def _announcement(code, title, day, url):
    return {"code": code, "name": code, "title": title, "date": day, "url": url}


def test_事件构建排序溢价与资格(monkeypatch):
    texts = {
        "u1": DONGXING_TEXT,
        "u2": XINDA_TEXT,
        "u3": KANGJIA_TEXT,
    }
    monkeypatch.setattr(service, "_fetch_pdf_text", lambda url: texts.get(url))
    announcements = [
        _announcement("601198", "东兴证券A股异议股东现金选择权实施公告", "2026-09-07", "u1"),
        _announcement("601059", "信达证券A股异议股东现金选择权实施的第一次提示性公告", "2026-09-14", "u2"),
        _announcement("000016", "关于主动终止公司股票上市现金选择权行权暨实施的公告", "2026-09-23", "u3"),
    ]
    quotes = {
        "601198": {"symbol": "601198", "name": "东兴证券", "current_price": 13.06, "quote_date": "2026-10-30"},
        "000016": {"symbol": "000016", "name": "*ST康佳A", "current_price": 2.46, "quote_date": "2026-10-30"},
    }

    events = service._build_events(announcements, quotes, date(2026, 11, 2))

    by_code = {item["code"]: item for item in events}
    assert set(by_code) == {"000016", "601198", "601059"}
    kangjia = by_code["000016"]
    dongxing = by_code["601198"]
    xinda = by_code["601059"]
    assert kangjia["status"] == "ended"
    assert kangjia["method"] == "delisting"
    assert kangjia["eligibility"] == "all"
    assert kangjia["premium_pct"] == 0.81
    assert dongxing["method"] == "merger"
    assert dongxing["basis"] == "quote"
    assert dongxing["premium_pct"] == -0.15
    # 信达无行情，回退公告参考价 15.56，溢价按参考价计算。
    assert xinda["basis"] == "reference"
    assert xinda["reference_price"] == 15.56
    assert xinda["premium_pct"] == 14.07


def test_过期的已结束事件被过滤(monkeypatch):
    monkeypatch.setattr(service, "_fetch_pdf_text", lambda url: DONGXING_TEXT)
    announcements = [
        _announcement("601198", "东兴证券A股异议股东现金选择权实施公告", "2026-09-07", "u1")
    ]

    events = service._build_events(announcements, {}, date(2027, 3, 1))

    assert events == []


def test_构建payload汇总与来源():
    events = [
        {"status": "ended", "premium_pct": 9.0, "announcement_date": "2026-09-23", "window_end": "2026-10-13"},
        {"status": "active", "premium_pct": 3.2, "announcement_date": "2026-09-07", "window_end": "2026-11-20", "days_left": 18},
        {"status": "upcoming", "premium_pct": 5.6, "announcement_date": "2026-10-20", "window_start": "2026-11-10"},
    ]

    payload = service.build_payload(events, date(2026, 11, 2), error="")

    assert payload["as_of"] == "2026-11-02"
    assert payload["summary"] == {
        "active": 1,
        "upcoming": 1,
        "recent_ended": 1,
        "best_premium_pct": 5.6,
    }
    assert [item["status"] for item in payload["events"]] == ["active", "upcoming", "ended"]
    assert payload["sources"][0]["id"] == "cninfo"
    assert payload["sources"][0]["latest"] == "2026-10-20"
    assert payload["error"] == ""


def test_抓取失败时payload带错误(monkeypatch):
    def fail(today):
        raise RuntimeError("network down")

    monkeypatch.setattr(service, "fetch_cninfo_announcements", fail)
    monkeypatch.setattr(service, "_quote_map", lambda codes: {})

    events, error = service._load_events(date(2026, 11, 2))
    payload = service.build_payload(events, date(2026, 11, 2), error=error)

    assert events == []
    assert error == "fetch_failed"
    assert payload["error"] == "fetch_failed"
    assert payload["summary"]["active"] == 0


def test_现金选择权接口转发(monkeypatch):
    calls = []

    def fake_payload(force=False):
        calls.append(force)
        return {"events": [], "summary": {"active": 0}}

    monkeypatch.setattr(service, "get_payload", fake_payload)
    app = FastAPI()
    app.include_router(recommendations.router, prefix="/api/recommendations")
    client = TestClient(app)

    listing = client.get("/api/recommendations/cash-options")
    refreshed = client.post("/api/recommendations/cash-options/refresh")

    assert listing.status_code == 200
    assert refreshed.status_code == 200
    assert listing.json()["summary"]["active"] == 0
    assert calls == [False, True]
