"""每日晨报：数据解析、提要拆分、非交易日跳过、Bark 直连推送。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.modules.automation import morning_report as mr
from src.modules.automation.base import AgentContext
from src.platform.notifications import bark
from src.platform.notifications import channels as notify_channels
from src.platform.runtime.config import AppConfig, Settings

# 收集阶段捕获真实实现（conftest 会在 setup 阶段把 bark.send 换成 no-op）
_REAL_BARK_SEND = bark.send


def _context(**settings_overrides) -> AgentContext:
    settings = Settings(**settings_overrides)
    return AgentContext(
        ai_client=Mock(chat=AsyncMock(return_value="")),
        notifier=Mock(),
        config=AppConfig(settings=settings, watchlist=[]),
    )


def _sample_data() -> dict:
    return {
        "us": ["道琼斯 +0.5%（来源：新浪行情）"],
        "tech": ["英伟达 100 (+1%)"],
        "cn": ["阿里巴巴 80 (+0.5%)"],
        "comm": ["纽约黄金 2600 (+0.50%)"],
        "fx": ["离岸人民币(USD/CNH) 7.1"],
        "nikkei": "日经225 +0.3%",
        "news": ["11-10 08:00 测试快讯"],
        "ashare": ["上证指数 +0.10%"],
        "coal": ["CCTD秦皇岛动力煤5500 700元/吨"],
        "alu": ["沪铝主力 20000 (+0.50%)"],
        "timestamp": "2026-11-10T08:00:00",
    }


def test_sina_hq_parses_gb18030_lines(monkeypatch):
    """新浪行情文本按 code 解析为字段列表，空行/空值跳过。"""
    text = (
        'var hq_str_gb_dia="道琼斯,100.5,0.50,2026-11-10";\n'
        'var hq_str_gb_qqq="";\n'
        'var hq_str_hf_GC="2600.5,,2601,2602,2610,2590,08:00:00,2580.0,2595,0,0,0,0,纽约黄金";\n'    )
    monkeypatch.setattr(mr, "market_get", lambda *a, **k: text)
    d = mr._sina_hq(["gb_dia", "gb_qqq", "hf_GC"])
    assert d["gb_dia"][2] == "0.50"
    assert "gb_qqq" not in d
    assert d["hf_GC"][13] == "纽约黄金"


def test_us_market_missing_items_marked_unavailable(monkeypatch):
    """未获取的行情项标注「暂未获取」，不编造。"""
    monkeypatch.setattr(mr, "_sina_hq", lambda codes: {})
    us, tech, cn = mr._fetch_us_market()
    assert all("暂未获取" in line for line in us + tech + cn)
    assert len(us) == 3 and len(tech) == 6 and len(cn) == 4


def test_commodities_change_pct(monkeypatch):
    """商品涨跌幅按最新价与昨收（字段7）计算。"""
    fields = [""] * 14
    fields[0], fields[7], fields[13] = "2600", "2580", "纽约黄金"
    monkeypatch.setattr(mr, "_sina_hq", lambda codes: {c: fields for c in codes})
    lines = mr._fetch_commodities()
    assert lines[0] == "纽约黄金 2600 (+0.78%)"


def test_coal_prices_come_from_database(monkeypatch):
    """动力煤价格读取煤炭价格页维护的数据库数据，不再抓 CCTD。"""
    from src.modules.market import coal_ltc_service, coal_price_service

    monkeypatch.setattr(
        coal_price_service, "load_price_rows",
        lambda db: {"2026-11-05": 1015.0, "2026-11-06": 1018.0},
    )
    monkeypatch.setattr(
        coal_ltc_service, "load_points",
        lambda db: [{"month": "2026-11", "price_5500": 727.0, "price_5000": 661.0, "price_4500": 595.0}],
    )
    lines = mr._fetch_coal_from_db()
    assert lines[0] == "CCI5500 动力煤 1018元/吨（2026-11-06，较前值 +0.30%，来源：本地数据库）"
    assert lines[1] == "长协价 2026-11：5500大卡 727元/吨，5000大卡 661元/吨，4500大卡 595元/吨"


def test_coal_prices_marked_unavailable_when_db_empty(monkeypatch):
    """数据库没有煤炭数据时标注暂未获取。"""
    from src.modules.market import coal_ltc_service, coal_price_service

    monkeypatch.setattr(coal_price_service, "load_price_rows", lambda db: {})
    monkeypatch.setattr(coal_ltc_service, "load_points", lambda db: [])
    assert mr._fetch_coal_from_db() == ["CCI5500 动力煤：暂未获取", "动力煤长协价：暂未获取"]


def test_sina_7x24_stops_at_window_edge(monkeypatch):
    """7x24 翻页遇到超出时间窗的快讯即停止。"""
    import datetime

    now = datetime.datetime.now()
    fresh = now.strftime("%Y-%m-%d %H:%M:%S")
    old = (now - datetime.timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    pages = {
        1: {"result": {"data": {"feed": {"list": [
            {"create_time": fresh, "rich_text": "新鲜快讯"},
        ]}}}},
        2: {"result": {"data": {"feed": {"list": [
            {"create_time": old, "rich_text": "过期快讯"},
        ]}}}},
    }
    calls = []

    def fake_get(url, *, params, **kwargs):
        calls.append(params["page"])
        return pages[params["page"]]

    monkeypatch.setattr(mr, "market_get", fake_get)
    items = mr._sina_7x24(hours=24, max_pages=40)
    assert [t for _, t in items] == ["新鲜快讯"]
    assert calls == [1, 2]


def test_collect_skips_non_trading_day(monkeypatch):
    """非交易日直接跳过，不发起任何抓取。"""
    monkeypatch.setattr(mr, "is_trading_day", lambda market: False)

    def fail(*a, **k):
        raise AssertionError("非交易日不应抓取")

    monkeypatch.setattr(mr, "_fetch_us_market", fail)
    data = asyncio.run(mr.MorningReportAgent().collect(_context()))
    assert data["skipped"] is True


def test_build_prompt_contains_all_sections():
    """Prompt 包含全部数据分区，系统提示词来自模板文件。"""
    system, user = mr.MorningReportAgent().build_prompt(_sample_data(), _context())
    assert "A股盘前策略分析师" in system
    for section in ["隔夜美股", "商品", "汇率与利率", "亚太早盘", "国内快讯", "A股昨日", "动力煤", "电解铝"]:
        assert section in user
    assert "测试快讯" in user


def test_analyze_splits_subtitle_and_saves_history(monkeypatch):
    """首行提要与正文拆分，标题带日期星期，全文存历史记录。"""
    saved = {}
    monkeypatch.setattr(mr, "save_analysis", lambda **kw: saved.update(kw) or True)
    ctx = _context()
    ctx.ai_client.chat = AsyncMock(return_value="美股收涨，关注算力\n\n**一、隔夜全球市场**\n· 道指 +0.5%")
    result = asyncio.run(mr.MorningReportAgent().analyze(ctx, _sample_data()))
    assert result.raw_data["subtitle"] == "美股收涨，关注算力"
    assert result.notify_content.startswith("**一、隔夜全球市场**")
    assert result.title.startswith("【A股晨报】")
    assert "星期" in result.title
    assert saved["agent_name"] == "morning_report"
    assert saved["stock_symbol"] == "*"
    assert saved["content"] == result.content


def test_analyze_skipped_does_not_notify(monkeypatch):
    """非交易日结果不触发通知。"""
    agent = mr.MorningReportAgent()
    result = asyncio.run(agent.analyze(_context(), {"skipped": True}))
    assert result.raw_data["skipped"] is True
    assert asyncio.run(agent.should_notify(result)) is False


def test_send_notification_uses_bark_channel_config(monkeypatch):
    """晨报走通知渠道里的 Bark 配置：副标题、分组「晨报」、历史页链接。"""
    sent = {}

    async def fake_send(key, title, body, **kwargs):
        sent.update(key=key, title=title, body=body, **kwargs)
        return {"success": True}

    monkeypatch.setattr(mr.bark, "send", fake_send)
    monkeypatch.setattr(
        mr, "resolve_bark_channel",
        lambda name: SimpleNamespace(type="bark", config={"device_key": "test-key", "server_url": "https://bark.example"}),
    )
    ctx = _context(public_base_url="http://192.168.18.29:8000/")
    result = mr.AnalysisResult(
        agent_name="morning_report", title="【A股晨报】11月10日 星期一",
        content="提要\n\n正文", notify_content="正文", raw_data={"subtitle": "提要"},
    )
    out = asyncio.run(mr.MorningReportAgent().send_notification(ctx, result))
    assert out["success"] is True
    assert sent["key"] == "test-key"
    assert sent["server"] == "https://bark.example"
    assert sent["body"] == "正文"
    assert sent["subtitle"] == "提要"
    assert sent["group"] == "晨报"
    assert sent["url"] == "http://192.168.18.29:8000/history"


def test_send_notification_without_base_url_has_no_link(monkeypatch):
    """未配置外部地址时不带点击链接。"""
    sent = {}

    async def fake_send(key, title, body, **kwargs):
        sent.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(mr.bark, "send", fake_send)
    monkeypatch.setattr(
        mr, "resolve_bark_channel",
        lambda name: SimpleNamespace(type="bark", config={"device_key": "test-key"}),
    )
    ctx = _context(public_base_url="")
    result = mr.AnalysisResult(agent_name="morning_report", title="t", content="b")
    asyncio.run(mr.MorningReportAgent().send_notification(ctx, result))
    assert sent["url"] == ""
    assert sent["server"] == bark.DEFAULT_SERVER


def test_send_notification_without_bark_channel_skips(monkeypatch):
    """未绑定/未启用 Bark 渠道时视为关闭推送，不发请求也不报错。"""
    monkeypatch.setattr(mr, "resolve_bark_channel", lambda name: None)
    called = False

    async def fake_send(*args, **kwargs):
        nonlocal called
        called = True
        return {"success": True}

    monkeypatch.setattr(mr.bark, "send", fake_send)
    result = mr.AnalysisResult(agent_name="morning_report", title="t", content="b")
    out = asyncio.run(mr.MorningReportAgent().send_notification(_context(), result))
    assert out == {"success": False, "skipped": "no_bark_channel"}
    assert called is False


def test_resolve_bark_channel_skips_other_types_and_empty_keys(monkeypatch):
    """只挑 Bark 渠道，且必须带 device_key。"""
    monkeypatch.setattr(
        notify_channels, "resolve_notify_channels",
        lambda *args, **kwargs: [
            SimpleNamespace(type="telegram", config={"bot_token": "x"}),
            SimpleNamespace(type="bark", config={"device_key": ""}),
            SimpleNamespace(type="bark", config={"device_key": "k"}),
        ],
    )
    assert notify_channels.resolve_bark_channel("morning_report").config["device_key"] == "k"


def test_bark_send_requires_key(monkeypatch):
    """未配置 key 时返回失败而不是发请求。"""
    monkeypatch.setattr(bark, "send", _REAL_BARK_SEND)
    out = asyncio.run(bark.send("", "标题", "正文"))
    assert out["success"] is False
    assert "Bark 密钥" in out["error"]


def test_bark_send_payload_and_truncation(monkeypatch):
    """Bark payload 含副标题/分组/链接；超长正文按字节截断。"""
    monkeypatch.setattr(bark, "send", _REAL_BARK_SEND)
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"code": 200, "message": "success"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(bark.httpx, "AsyncClient", FakeClient)
    body = "字" * 3000  # 9000 字节 > 4800 上限
    out = asyncio.run(bark.send(
        "key123", "标题", body, subtitle="提要", url="http://x/history", group="晨报",
    ))
    assert out["success"] is True
    assert captured["url"] == "https://api.day.app/key123"
    payload = captured["payload"]
    assert payload["subtitle"] == "提要"
    assert payload["group"] == "晨报"
    assert payload["url"] == "http://x/history"
    assert len(payload["body"].encode("utf-8")) <= bark.MAX_BODY + len("\n…(内容过长已截断)".encode("utf-8"))
    assert payload["body"].endswith("…(内容过长已截断)")