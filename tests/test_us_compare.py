"""美股收益对比：标的解析、区间归一化、对齐与接口。"""

from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.market import us_compare_service
from src.modules.market.api import etf_shares as etf_shares_api


@pytest.fixture(autouse=True)
def no_live_requests(monkeypatch):
    """默认不访问腾讯实时行情，需要报价的用例显式注入。"""
    monkeypatch.setattr(us_compare_service, "_fetch_lines", lambda tsymbols: [])

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 6, 8, tzinfo=tz)

    monkeypatch.setattr(us_compare_service, "datetime", FixedDatetime)
    yield


def bar(day: str, close: float):
    return us_compare_service._Point(day, close)


def quote(day: str, close: float, prev_close: float, at: str = "16:00:02"):
    stamp = datetime.fromisoformat(f"{day} {at}")
    return us_compare_service._Quote(day, stamp, close, prev_close)


def tencent_line(
    tsymbol: str, name: str, code: str, close: str, prev: str, stamp: str
):
    fields = ["200", name, code, close, prev, "0"]
    fields += ["0"] * 25  # 补齐到第 30 个字段
    fields[30] = stamp
    return f'v_{tsymbol}="{"~".join(fields)}"'


def test_Nasdaq历史解析并升序排列():
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {"date": "01/06/2026", "close": "$120.50"},
                    {"date": "01/02/2026", "close": "$23,235.63"},
                    {"date": "01/05/2026", "close": "N/A"},
                    "bad-row",
                ]
            }
        }
    }

    points = us_compare_service.parse_nasdaq_history(payload)

    assert [(point.date, point.close) for point in points] == [
        ("2026-01-02", 23235.63),
        ("2026-01-06", 120.5),
    ]


def test_标的解析去重并转大写():
    assert us_compare_service.parse_symbols(" schd, SP500,,schd ") == (
        "SCHD",
        "SP500",
    )


def test_标的解析拒绝非法与超量():
    with pytest.raises(ValueError):
        us_compare_service.parse_symbols("")
    with pytest.raises(ValueError):
        us_compare_service.parse_symbols("SP 500")
    with pytest.raises(ValueError):
        us_compare_service.parse_symbols(",".join(f"AAA{i}" for i in range(9)))


def test_区间校验():
    assert us_compare_service.normalize_range("YTD") == "ytd"
    assert us_compare_service.normalize_range("3y") == "3y"
    with pytest.raises(ValueError):
        us_compare_service.normalize_range("5y")


def test_年初至今以去年最后一个收盘价为基准():
    bars = [
        bar("2025-12-30", 100.0),
        bar("2025-12-31", 110.0),
        bar("2026-01-02", 121.0),
        bar("2026-01-05", 99.0),
    ]

    values, base_date = us_compare_service._normalized(
        bars, date(2026, 1, 1)
    )

    assert base_date == "2025-12-31"
    assert values == {"2026-01-02": 10.0, "2026-01-05": -10.0}


def test_历史不足区间起点时以首个收盘价为基准():
    bars = [bar("2026-02-02", 50.0), bar("2026-02-03", 55.0)]

    values, base_date = us_compare_service._normalized(
        bars, date(2026, 1, 1)
    )

    assert base_date == "2026-02-02"
    assert values == {"2026-02-02": 0.0, "2026-02-03": 10.0}


def test_对齐缺失日不补值(monkeypatch):
    rows = {
        "SCHD": [
            bar("2025-12-31", 100.0),
            bar("2026-01-02", 110.0),
            bar("2026-01-05", 120.0),
        ],
        "QQQ": [
            bar("2025-12-31", 50.0),
            bar("2026-01-02", 55.0),
            bar("2026-01-06", 60.0),
        ],
    }
    monkeypatch.setattr(
        us_compare_service,
        "_symbol_points",
        lambda code, force=False: rows[code],
    )

    payload = us_compare_service.build_payload(
        ("SCHD", "QQQ"), "ytd", today=date(2026, 1, 10), force=True
    )

    assert payload["dates"] == ["2026-01-02", "2026-01-05", "2026-01-06"]
    assert payload["series"] == {
        "SCHD": [10.0, 20.0, None],
        "QQQ": [10.0, None, 20.0],
    }
    assert payload["symbols"] == ["SCHD", "QQQ"]
    assert payload["items"][0] == {
        "code": "SCHD",
        "label": "嘉信美国红利股ETF",
        "start_date": "2025-12-31",
        "latest_date": "2026-01-05",
        "latest": 20.0,
        "points": 2,
    }
    assert payload["items"][1]["latest_date"] == "2026-01-06"
    assert payload["as_of"] == "2026-01-06"
    assert payload["coverage"] == {
        "trading_days": 1,
        "target": 3,
        "complete": False,
    }
    assert payload["range_label"] == "年初至今"
    assert payload["metric"]["id"] == "price_return"
    assert "不含分红" in payload["metric"]["label"]
    assert [source["id"] for source in payload["sources"]] == ["nasdaq"]


def test_数据源按标的类型返回(monkeypatch):
    rows = {
        "SCHD": [bar("2025-12-31", 100.0), bar("2026-01-02", 110.0)],
        "SP500": [bar("2025-12-31", 6000.0), bar("2026-01-02", 6090.0)],
    }
    monkeypatch.setattr(
        us_compare_service,
        "_symbol_points",
        lambda code, force=False: rows[code],
    )

    payload = us_compare_service.build_payload(
        ("SCHD", "SP500"), "ytd", today=date(2026, 1, 10), force=True
    )

    assert [source["id"] for source in payload["sources"]] == [
        "nasdaq",
        "tencent",
    ]
    assert all(source["name"] for source in payload["sources"])


def test_未收录代码沿用代码作为名称(monkeypatch):
    rows = {"WHO": [bar("2025-12-31", 100.0), bar("2026-01-02", 105.0)]}
    monkeypatch.setattr(
        us_compare_service,
        "_symbol_points",
        lambda code, force=False: rows[code],
    )

    payload = us_compare_service.build_payload(
        ("WHO",), "ytd", today=date(2026, 1, 10), force=True
    )

    assert payload["items"][0]["label"] == "WHO"


def test_无数据的标的记入缺失(monkeypatch):
    rows = {"SCHD": [bar("2025-12-31", 100.0), bar("2026-01-02", 110.0)]}
    monkeypatch.setattr(
        us_compare_service,
        "_symbol_points",
        lambda code, force=False: rows.get(code, []),
    )

    payload = us_compare_service.build_payload(
        ("SCHD", "NOPE"), "ytd", today=date(2026, 1, 10), force=True
    )

    assert payload["symbols"] == ["SCHD"]
    assert payload["missing"] == ["NOPE"]
    assert payload["series"] == {"SCHD": [10.0]}


def test_接口转发标的与区间(monkeypatch):
    captured = {}

    def fake_payload(symbols_raw, range_raw, *, force=False):
        captured["args"] = (symbols_raw, range_raw, force)
        return {"dates": [], "series": {}}

    monkeypatch.setattr(
        us_compare_service, "get_compare_payload", fake_payload
    )
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")

    response = TestClient(app).get(
        "/api/etf-shares/us-compare?symbols=SCHD,QQQ&range=1y"
    )

    assert response.status_code == 200
    assert captured["args"] == ("SCHD,QQQ", "1y", False)


def test_接口参数非法返回400(monkeypatch):
    def fake_payload(symbols_raw, range_raw, *, force=False):
        raise ValueError("区间仅支持 ytd / 1y / 3y")

    monkeypatch.setattr(
        us_compare_service, "get_compare_payload", fake_payload
    )
    app = FastAPI()
    app.include_router(etf_shares_api.router, prefix="/api/etf-shares")

    response = TestClient(app).get(
        "/api/etf-shares/us-compare?symbols=SCHD&range=5y"
    )

    assert response.status_code == 400


def test_腾讯报价解析符号收盘与昨收():
    etf = tencent_line(
        "usQQQ", "纳指100ETF-Invesco", "QQQ.OQ", "647.05", "691.95",
        "2026-11-05 16:00:02",
    )
    index = tencent_line(
        "us.INX", "标普500", ".INX", "7353.73", "7688.62",
        "2026-11-05 16:34:08",
    )

    assert us_compare_service.parse_tencent_quote(etf) == (
        "usQQQ",
        us_compare_service._Quote(
            "2026-11-05",
            datetime(2026, 11, 5, 16, 0, 2),
            647.05,
            691.95,
        ),
    )
    assert us_compare_service.parse_tencent_quote(index)[0] == "us.INX"
    assert us_compare_service.parse_tencent_quote('v_usNOPE=""') is None
    assert us_compare_service.parse_tencent_quote("") is None


def test_报价补齐历史未发布的最后一日():
    points = [bar("2026-11-03", 100.0), bar("2026-11-04", 110.0)]

    result, changed = us_compare_service._reconcile_latest(
        points,
        quote("2026-11-05", 121.0, 110.0),
        datetime(2026, 11, 6, 8, 0),
    )

    assert changed is True
    assert [point.date for point in result] == [
        "2026-11-03",
        "2026-11-04",
        "2026-11-05",
    ]
    assert result[-1].close == 121.0


def test_盘中报价不补值():
    """当天（美东）收盘前拿到的只是盘中价，不能当作收盘价写进曲线。"""
    points = [bar("2026-11-03", 100.0), bar("2026-11-04", 110.0)]

    intraday = us_compare_service._reconcile_latest(
        points,
        quote("2026-11-05", 121.0, 110.0, at="11:30:00"),
        datetime(2026, 11, 5, 11, 31),
    )
    same_day_partial = us_compare_service._reconcile_latest(
        [bar("2026-11-03", 100.0), bar("2026-11-04", 110.0), bar("2026-11-05", 111.0)],
        quote("2026-11-05", 121.0, 110.0, at="11:30:00"),
        datetime(2026, 11, 5, 11, 31),
    )

    assert intraday == (points, False)
    assert same_day_partial[1] is False


def test_昨收与历史对不上时不补值():
    """拆股复权口径不同：腾讯昨收是未复权价，不能拿来补 Nasdaq 复权序列。"""
    points = [bar("2023-05-31", 100.0), bar("2023-06-01", 110.0)]

    result, changed = us_compare_service._reconcile_latest(
        points,
        quote("2023-06-02", 36.0, 35.6),
        datetime(2023, 6, 3, 8, 0),
    )

    assert changed is False
    assert result == points


def test_同一天坏值按报价修复():
    points = [bar("2026-11-04", 28437.17), bar("2026-11-05", 10.75)]

    result, changed = us_compare_service._reconcile_latest(
        points,
        quote("2026-11-05", 26629.81, 28437.17, at="17:15:59"),
        datetime(2026, 11, 6, 8, 0),
    )

    assert changed is True
    assert [(point.date, point.close) for point in result] == [
        ("2026-11-04", 28437.17),
        ("2026-11-05", 26629.81),
    ]


def test_同一天报价与历史一致时不改写():
    points = [bar("2026-11-04", 110.0), bar("2026-11-05", 121.0)]

    result, changed = us_compare_service._reconcile_latest(
        points,
        quote("2026-11-05", 121.2, 110.0),
        datetime(2026, 11, 6, 8, 0),
    )

    assert changed is False
    assert result == points


def test_别名标的按腾讯符号回填报价(monkeypatch):
    lines = [
        tencent_line(
            "usSCHD", "红利股ETF-Schwab", "SCHD.AM", "35.42", "36.21",
            "2026-11-05 16:00:02",
        ),
        tencent_line(
            "us.INX", "标普500", ".INX", "7353.73", "7688.62",
            "2026-11-05 16:34:08",
        ),
    ]
    requested = {}

    def fake_fetch(tsymbols):
        requested["tsymbols"] = tsymbols
        return lines

    monkeypatch.setattr(us_compare_service, "_fetch_lines", fake_fetch)

    quotes = us_compare_service._latest_quotes(("SCHD", "SP500", "SPX"))

    assert requested["tsymbols"] == ["usSCHD", "us.INX"]
    assert [quotes["SCHD"].close, quotes["SP500"].close] == [35.42, 7353.73]
    assert quotes["SPX"] == quotes["SP500"]


def test_接口用实时收盘补齐最新一天(monkeypatch):
    rows = {
        "QQQ": [bar("2025-12-31", 100.0), bar("2026-11-04", 110.0)],
        "NDX": [bar("2025-12-31", 100.0), bar("2026-11-04", 110.0)],
    }
    monkeypatch.setattr(
        us_compare_service,
        "_symbol_points",
        lambda code, force=False: rows[code],
    )
    monkeypatch.setattr(
        us_compare_service,
        "_latest_quotes",
        lambda codes, force=False: {
            "QQQ": quote("2026-11-05", 121.0, 110.0),
            "NDX": quote("2026-11-05", 100.0, 110.0),
        },
    )

    payload = us_compare_service.build_payload(
        ("QQQ", "NDX"), "ytd", today=date(2026, 11, 6), force=True
    )

    assert payload["dates"][-1] == "2026-11-05"
    assert payload["as_of"] == "2026-11-05"
    assert payload["series"]["QQQ"][-1] == 21.0
    assert payload["series"]["NDX"][-1] == 0.0
    assert payload["coverage"]["complete"] is True
    assert "tencent_quote" in [source["id"] for source in payload["sources"]]


def test_报价取不到时曲线照旧(monkeypatch):
    rows = {"QQQ": [bar("2025-12-31", 100.0), bar("2026-11-04", 110.0)]}
    monkeypatch.setattr(
        us_compare_service,
        "_symbol_points",
        lambda code, force=False: rows[code],
    )
    monkeypatch.setattr(
        us_compare_service, "_latest_quotes", lambda codes, force=False: {}
    )

    payload = us_compare_service.build_payload(
        ("QQQ",), "ytd", today=date(2026, 11, 6), force=True
    )

    assert payload["dates"] == ["2026-11-04"]
    assert [source["id"] for source in payload["sources"]] == ["nasdaq"]


@pytest.mark.parametrize("close,prev", [("nan", "100"), ("100", "inf"), ("0", "100"), ("bad", "100")])
def test_报价拒绝非法价格(close, prev):
    line = tencent_line("usQQQ", "QQQ", "QQQ.OQ", close, prev, "2026-11-05 16:00:02")
    assert us_compare_service.parse_tencent_quote(line) is None


@pytest.mark.parametrize(
    "stamp,now,closed",
    [
        ("2026-11-05 11:30:00", "2026-11-06 08:00:00", False),
        ("2026-11-05 16:00:02", "2026-11-05 08:00:00", False),
        ("2026-11-06 16:00:02", "2026-11-05 17:00:00", False),
        ("2026-11-05 16:00:02", "2026-11-05 16:01:00", True),
        ("2026-07-01 16:00:02", "2026-07-01 16:01:00", True),
    ],
)
def test_收盘判定拒绝陈旧盘中及未来报价(stamp, now, closed):
    at = datetime.fromisoformat(stamp)
    value = us_compare_service._Quote(at.date().isoformat(), at, 110, 100)
    now_et = datetime.fromisoformat(now).replace(tzinfo=us_compare_service._ET)
    assert us_compare_service._is_closed_session(value, now_et) is closed


def test_刷新绕过历史和报价缓存(monkeypatch):
    history_calls = []
    quote_calls = []

    def history(code):
        history_calls.append(code)
        return [bar("2025-12-31", 100), bar("2026-11-04", 110)]

    def latest(symbols):
        quote_calls.append(symbols)
        close = "121" if len(quote_calls) == 1 else "122"
        return [tencent_line("usQQQ", "QQQ", "QQQ.OQ", close, "110", "2026-11-05 16:00:02")]

    monkeypatch.setattr(us_compare_service, "_fetch_symbol_points", history)
    monkeypatch.setattr(us_compare_service, "_fetch_lines", latest)
    first = us_compare_service.build_payload(("QQQ",), "ytd")
    cached = us_compare_service.build_payload(("QQQ",), "ytd")
    assert first == cached
    assert len(history_calls) == len(quote_calls) == 1
    refreshed = us_compare_service.build_payload(("QQQ",), "ytd", force=True)
    assert len(history_calls) == len(quote_calls) == 2
    assert first["series"]["QQQ"][-1] == 21.0
    assert refreshed["series"]["QQQ"][-1] == 22.0


def test_缺失日仍为空且不修改历史缓存(monkeypatch):
    points = [bar("2025-12-31", 100), bar("2026-11-03", 110)]
    monkeypatch.setattr(us_compare_service, "_symbol_points", lambda code, force=False: points)
    monkeypatch.setattr(
        us_compare_service, "_latest_quotes",
        lambda codes, force=False: {"QQQ": quote("2026-11-05", 121, 110)},
    )
    payload = us_compare_service.build_payload(("QQQ",), "ytd")
    assert payload["dates"] == ["2026-11-03", "2026-11-05"]
    assert points == [bar("2025-12-31", 100), bar("2026-11-03", 110)]


@pytest.mark.parametrize("broken_tail", [False, True])
def test_报价过期后故障不撤销核验过的末日(monkeypatch, broken_tail):
    points = [bar("2025-12-31", 100), bar("2026-11-04", 110)]
    if broken_tail:
        points.append(bar("2026-11-05", 0.01))
    monkeypatch.setattr(us_compare_service, "_fetch_symbol_points", lambda code: points)
    monkeypatch.setattr(
        us_compare_service, "_fetch_lines",
        lambda symbols: [tencent_line("us.NDX", "NDX", ".NDX", "121", "110", "2026-11-05 16:00:02")],
    )
    first = us_compare_service.build_payload(("NDX",), "ytd")
    assert first["series"]["NDX"][-1] == 21.0
    import time
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock + 301)
    monkeypatch.setattr(us_compare_service, "_fetch_lines", lambda symbols: [])
    after_expiry = us_compare_service.build_payload(("NDX",), "ytd")
    after_refresh = us_compare_service.build_payload(("NDX",), "ytd", force=True)
    assert after_expiry == after_refresh == first
    points[:] = [bar("2025-12-31", 100), bar("2026-11-04", 110), bar("2026-11-05", 121)]
    official = us_compare_service.build_payload(("NDX",), "ytd", force=True)
    assert official["series"]["NDX"][-1] == 21.0
    assert "tencent_quote" not in [s["id"] for s in official["sources"]]


@pytest.mark.parametrize("broken_tail", [False, True])
def test_重建存储后历史和核验收盘可离线读取(monkeypatch, broken_tail):
    from src.modules.market.statistics_store import StatisticsStore

    points = [bar("2025-12-31", 100), bar("2026-11-04", 110)]
    if broken_tail:
        points.append(bar("2026-11-05", 0.01))
    monkeypatch.setattr(us_compare_service, "_fetch_symbol_points", lambda code: points)
    monkeypatch.setattr(us_compare_service, "_fetch_lines", lambda symbols: [
        tencent_line("us.NDX", "NDX", ".NDX", "121", "110", "2026-11-05 16:00:02")
    ])
    first = us_compare_service.build_payload(("NDX",), "ytd", force=True)
    monkeypatch.setattr(us_compare_service, "_store", StatisticsStore("usCompare", 1800))

    def fail(*args):
        raise RuntimeError("controlled upstream failure")

    monkeypatch.setattr(us_compare_service, "_fetch_symbol_points", fail)
    monkeypatch.setattr(us_compare_service, "_fetch_lines", fail)
    assert us_compare_service.build_payload(("NDX",), "ytd", force=True) == first
    assert first["series"]["NDX"][-1] == 21.0


def test_持久化收盘补丁超过七天仍保留且复权变化不混用(monkeypatch):
    from src.modules.market.statistics_store import StatisticsStore
    import time

    points = [bar("2025-12-31", 100), bar("2026-11-04", 110)]
    monkeypatch.setattr(us_compare_service, "_fetch_symbol_points", lambda code: points)
    monkeypatch.setattr(us_compare_service, "_latest_quotes", lambda codes, force=False: {"QQQ": quote("2026-11-05", 121, 110)})
    first = us_compare_service.build_payload(("QQQ",), "ytd", force=True)
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock + 8 * 86400)
    monkeypatch.setattr(us_compare_service, "_store", StatisticsStore("usCompare", 1800))
    monkeypatch.setattr(us_compare_service, "_latest_quotes", lambda codes, force=False: {})
    monkeypatch.setattr(us_compare_service, "_fetch_symbol_points", lambda code: [])
    assert us_compare_service.build_payload(("QQQ",), "ytd", force=True) == first
    split = [bar("2025-12-31", 50), bar("2026-11-04", 55), bar("2026-11-05", 60.5)]
    monkeypatch.setattr(us_compare_service, "_fetch_symbol_points", lambda code: split)
    after = us_compare_service.build_payload(("QQQ",), "ytd", force=True)
    assert after["series"]["QQQ"][-1] == 21.0
    assert "tencent_quote" not in [s["id"] for s in after["sources"]]
    assert us_compare_service._symbol_points("QQQ", force=False) == split


def test_没有本地历史时失败不虚构数据(monkeypatch):
    monkeypatch.setattr(us_compare_service, "_fetch_symbol_points", lambda code: [])
    result = us_compare_service.build_payload(("QQQ",), "ytd", force=True)
    assert result["dates"] == []
    assert result["missing"] == ["QQQ"]
