import marketdata.vendors.discovery as dv
from marketdata.types import HotBoard, HotStock


def test_hot_stocks_maps_fcodes(monkeypatch):
    # 东财 clist diff:f12=代码 f14=名称 f2=最新价 f3=涨跌幅 f6=成交额 f5=成交量
    payload = {
        "data": {
            "diff": [
                {"f12": "600519", "f14": "贵州茅台", "f2": 1700.5, "f3": 3.2, "f6": 85000000, "f5": 50000},
            ]
        }
    }
    captured = {}

    def fake_market_get(*args, **kwargs):
        captured["url"] = args[0]
        return payload

    monkeypatch.setattr(dv, "market_get", fake_market_get)
    out = dv.DiscoveryVendor().hot_stocks(market="CN", mode="turnover", limit=20)
    assert captured["url"] == "https://push2delay.eastmoney.com/api/qt/clist/get"
    assert len(out) == 1 and isinstance(out[0], HotStock)
    hs = out[0]
    assert hs.symbol == "600519"
    assert hs.market == "CN"
    assert hs.name == "贵州茅台"
    assert hs.price == 1700.5
    assert hs.change_pct == 3.2
    assert hs.turnover == 85000000
    assert hs.volume == 50000


def test_hot_stocks_normalizes_dict_diff(monkeypatch):
    # 东财 diff 有时是 dict(按 index 为 key)而非 list,需归一化。
    payload = {
        "data": {
            "diff": {
                "0": {"f12": "000001", "f14": "平安银行", "f2": 12.3, "f3": -1.1, "f6": 900000, "f5": 4000},
            }
        }
    }
    monkeypatch.setattr(dv, "market_get", lambda *a, **k: payload)
    out = dv.DiscoveryVendor().hot_stocks(market="CN", mode="turnover", limit=20)
    assert len(out) == 1
    assert out[0].symbol == "000001" and out[0].name == "平安银行"


def test_hot_boards_maps_fcodes(monkeypatch):
    # 板块 diff:f12=板块代码 f14=板块名称 f3=涨跌幅 f4=涨跌额 f6=成交额
    payload = {
        "data": {
            "diff": [
                {"f12": "BK0448", "f14": "白酒", "f2": 1.0, "f3": 2.5, "f4": 0.3, "f6": 12000000},
            ]
        }
    }
    monkeypatch.setattr(dv, "market_get", lambda *a, **k: payload)
    out = dv.DiscoveryVendor().hot_boards(market="CN", mode="gainers", limit=12)
    assert len(out) == 1 and isinstance(out[0], HotBoard)
    hb = out[0]
    assert hb.code == "BK0448"
    assert hb.name == "白酒"
    assert hb.change_pct == 2.5
    assert hb.change_amount == 0.3
    assert hb.turnover == 12000000


def test_board_stocks_maps_fcodes(monkeypatch):
    payload = {
        "data": {
            "diff": [
                {"f12": "600809", "f14": "山西汾酒", "f2": 200.1, "f3": 5.0, "f6": 3000000, "f5": 12000},
            ]
        }
    }
    monkeypatch.setattr(dv, "market_get", lambda *a, **k: payload)
    out = dv.DiscoveryVendor().board_stocks(board_code="BK0448", mode="gainers", limit=20)
    assert len(out) == 1 and isinstance(out[0], HotStock)
    hs = out[0]
    assert hs.symbol == "600809"
    assert hs.market == "CN"
    assert hs.name == "山西汾酒"
    assert hs.price == 200.1
    assert hs.change_pct == 5.0
    assert hs.turnover == 3000000
    assert hs.volume == 12000


def test_board_stocks_empty_code_returns_empty(monkeypatch):
    called = []
    monkeypatch.setattr(dv, "market_get", lambda *a, **k: called.append(1))
    assert dv.DiscoveryVendor().board_stocks(board_code="", mode="gainers", limit=20) == []
    assert called == []


def _hotmap_row(market: str, code: str, zdf: str, cje: str, zxj: str, zsz: str, zde: str = "0") -> str:
    # market|code|zde|zdf|zdf_3|zdf_5|zdf_10|zdf_20|zdf_60|zdf_nc|cje|hsl|zxj|ltsz|zsz|lb|zljzb
    return "|".join([market, code, zde, zdf, "0", "0", "0", "0", "0", "0", cje, "1.2", zxj, "0", zsz, "0", "0"])


def test_market_map_maps_stocks_and_breadth(monkeypatch):
    base = {
        "baseinfo": [
            "0|0|0|建设银行|CN|601939|",
            "0|1|0|长鑫科技|CN|688981|",
            "0|0|0|贵州茅台|CN|600519|",
        ],
        "bk2": ["银行Ⅱ|0|BK0475|1", "半导体|1|BK1036|2"],
    }
    quotes = {
        "quotetime": 1793950482,
        "data": [
            _hotmap_row("CN", "601939", "214", "100000000", "1000", "200000", "25"),
            _hotmap_row("CN", "688981", "-498", "200000000", "5000", "100000"),
            _hotmap_row("CN", "600519", "0", "300000000", "170000", "300000"),
            "ENCRYPTEDBLOB",  # 密文行,无分隔符
            _hotmap_row("CN", "999999", "100", "1", "1", "1"),  # baseinfo 缺失
        ],
    }
    calls = []

    def fake_market_get(url, **kwargs):
        calls.append((url, kwargs.get("host_key"), kwargs.get("timeout")))
        return base if "getquotebasedata" in url else quotes

    monkeypatch.setattr(dv, "market_get", fake_market_get)
    out = dv.DiscoveryVendor().market_map(market="CN", limit=2)

    assert len(calls) == 2
    assert all(host == "quote.eastmoney.com" for _, host, _ in calls)
    assert all(timeout == 20 for _, _, timeout in calls)
    assert [s.symbol for s in out.stocks] == ["600519", "601939"]
    top = out.stocks[1]
    assert top.name == "建设银行"
    assert top.industry == "银行Ⅱ"
    assert top.price == 10.0
    assert top.change_pct == 2.14
    assert top.change_amount == 0.25
    assert top.total_market_value == 200000 * 1_000_000
    assert out.stocks[0].change_pct == 0
    assert out.stocks[0].industry == "银行Ⅱ"
    # 涨跌家数按全量(不含缺失 meta 的行)统计,而不是按截断后的 limit。
    assert (out.total, out.up, out.flat, out.down) == (3, 1, 1, 1)
    assert out.amount == 600000000.0
    assert out.as_of == 1793950482


def test_market_map_only_supports_cn(monkeypatch):
    calls = []
    monkeypatch.setattr(dv, "market_get", lambda *a, **k: calls.append(1))
    out = dv.DiscoveryVendor().market_map(market="HK", limit=400)
    assert out.stocks == [] and out.total == 0
    assert calls == []


def test_market_map_raises_on_empty_payload(monkeypatch):
    monkeypatch.setattr(dv, "market_get", lambda *a, **k: {})
    try:
        dv.DiscoveryVendor().market_map()
    except RuntimeError as exc:
        assert "空数据" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_market_map_defaults_missing_values_to_none(monkeypatch):
    base = {"baseinfo": ["0|0|0|停牌股|CN|600001|"], "bk2": ["综合|0|BK0001|1"]}
    quotes = {"data": [_hotmap_row("CN", "600001", "5", "0", "-", "-")]}
    monkeypatch.setattr(dv, "market_get",
                        lambda url, **k: base if "getquotebasedata" in url else quotes)
    out = dv.DiscoveryVendor().market_map(market="CN", limit=400)
    stock = out.stocks[0]
    assert stock.price is None and stock.total_market_value is None
    assert stock.change_pct == 0.05
    assert out.amount is None
    assert out.as_of is None
