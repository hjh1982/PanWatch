"""每日晨报 Agent — 移植自 share/task/daily_report_push.py。

盘前抓取隔夜全球市场数据 → AI 按晨报模板生成 → 直连 Bark 推送（分组「晨报」，
副标题为一句话提要，点击跳转 PanWatch 历史页）。
数据源：新浪行情、新浪财经7x24、东财、CCTD、akshare（A股复盘/库存）。
无法获取的项按模板标注「暂未获取」，禁止编造。
"""

import asyncio
import datetime
import logging
import re
from pathlib import Path

from src.modules.automation.base import AgentContext, AnalysisResult, BaseAgent
from src.modules.research.analysis_history import save_analysis
from src.platform.marketdata.collectors.market_http import market_get
from src.platform.notifications import bark
from src.platform.notifications.channels import resolve_bark_channel
from src.platform.scheduling.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "morning_report.txt"

_SINA_HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
_ZHIBO_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/7x24/"}

US_INDICES = [("gb_dia", "道琼斯"), ("gb_qqq", "纳斯达克100"), ("gb_spy", "标普500")]
US_TECHS = [("gb_nvda", "英伟达"), ("gb_aapl", "苹果"), ("gb_msft", "微软"),
            ("gb_tsla", "特斯拉"), ("gb_googl", "谷歌"), ("gb_amd", "AMD")]
CN_STOCKS = [("gb_baba", "阿里巴巴"), ("gb_pdd", "拼多多"), ("gb_jd", "京东"), ("gb_ntes", "网易")]


def _sina_hq(codes: list[str]) -> dict[str, list[str]]:
    text = market_get(
        "https://hq.sinajs.cn/list=" + ",".join(codes),
        host_key="hq.sinajs.cn", headers=_SINA_HEADERS, encoding="gb18030",
        trust_env=False, log_label="晨报新浪行情",
    )
    out: dict[str, list[str]] = {}
    for line in (text or "").strip().splitlines():
        m = re.match(r'var hq_str_(\w+)="(.*)";', line.strip())
        if m and m.group(2):
            out[m.group(1)] = m.group(2).split(",")
    return out


def _fetch_us_market():
    d = _sina_hq([c for c, _ in US_INDICES] + [c for c, _ in US_TECHS] + [c for c, _ in CN_STOCKS])
    lines = []
    for code, name in US_INDICES:
        f = d.get(code)
        lines.append(f"{name} {f[2]}%（来源：新浪行情）" if f and f[0] else f"{name} 暂未获取")
    tech = []
    for code, name in US_TECHS:
        f = d.get(code)
        tech.append(f"{name} {f[1]} ({f[2]}%)" if f and f[0] else f"{name} 暂未获取")
    cns = []
    for code, name in CN_STOCKS:
        f = d.get(code)
        cns.append(f"{name} {f[1]} ({f[2]}%)" if f and f[0] else f"{name} 暂未获取")
    return lines, tech, cns


def _fetch_commodities():
    d = _sina_hq(["hf_GC", "hf_CL", "hf_HG", "hf_XAU"])
    lines = []
    for code in ["hf_GC", "hf_CL", "hf_HG", "hf_XAU"]:
        f = d.get(code)
        if f and f[0]:
            try:
                pct = (float(f[0]) - float(f[7])) / float(f[7]) * 100
            except (ValueError, ZeroDivisionError):
                pct = float("nan")
            lines.append(f"{f[13]} {f[0]} ({pct:+.2f}%)" if pct == pct else f"{f[13]} {f[0]}")
        else:
            lines.append("暂未获取")
    return lines  # 纽约黄金 / 纽约原油 / 美铜 / 伦敦金现货


def _fetch_fx():
    d = _sina_hq(["fx_susdcnh", "fx_susdcny"])
    lines = []
    cnh, cny = d.get("fx_susdcnh"), d.get("fx_susdcny")
    lines.append(f"离岸人民币(USD/CNH) {cnh[1]}（来源：新浪行情）" if cnh and cnh[1] else "离岸人民币 暂未获取")
    lines.append(f"在岸人民币(USD/CNY) {cny[1]}（来源：新浪行情）" if cny and cny[1] else "在岸人民币 暂未获取")
    lines.append("美元指数 暂未获取")
    r = market_get(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        host_key="push2.eastmoney.com",
        params={"fltt": "2", "fields": "f2,f3,f12,f14", "secids": "1.US10Y"},
        headers={"User-Agent": "Mozilla/5.0"}, parse="json", force_ipv4=True,
        trust_env=False, log_label="晨报美债收益率",
    )
    if r and r.get("data") and r["data"].get("f2"):
        lines.append(f"美债10年期收益率 {r['data']['f2']}%（来源：东财）")
    else:
        try:
            import akshare as ak
            start = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y%m%d")
            b = ak.bond_zh_us_rate(start_date=start).dropna(subset=["美国国债收益率10年"])
            row = b.iloc[-1]
            lines.append(f"美债10年期收益率 {row['美国国债收益率10年']}%（来源：中国债券信息网）")
        except Exception:
            lines.append("美债10年期收益率 暂未获取")
    return lines


def _fetch_nikkei():
    d = _sina_hq(["gb_ewj"])
    f = d.get("gb_ewj")
    return f"日经225 {f[2]}%（来源：新浪行情）" if f and f[0] else "日经225 暂未获取"


def _sina_7x24(hours: int, max_pages: int) -> list[tuple[datetime.datetime, str]]:
    """翻页抓取新浪7x24近 N 小时快讯，返回 (时间, 文本) 列表。"""
    now = datetime.datetime.now()
    since = now - datetime.timedelta(hours=hours)
    items: list[tuple[datetime.datetime, str]] = []
    for page in range(1, max_pages + 1):
        r = market_get(
            "https://zhibo.sina.com.cn/api/zhibo/feed",
            host_key="zhibo.sina.com.cn",
            params={"page": page, "page_size": 30, "zhibo_id": 152, "tag_id": 0, "dire": "f", "dpc": 1},
            headers=_ZHIBO_HEADERS, parse="json", retries=1,
            trust_env=False, log_label="晨报新浪7x24",
        )
        feed = ((r or {}).get("result") or {}).get("data", {}).get("feed", {}).get("list", [])
        if not feed:
            break
        expired = False
        for it in feed:
            t = (it.get("rich_text") or it.get("text") or "").strip()
            if not t:
                continue
            try:
                dt = datetime.datetime.strptime(it["create_time"][:19], "%Y-%m-%d %H:%M:%S")
            except (KeyError, ValueError, TypeError):
                continue
            if dt < since:
                expired = True
                break
            if dt <= now:
                items.append((dt, t))
        if expired:
            break
    return items


def _fetch_news():
    try:
        items = _sina_7x24(hours=24, max_pages=40)
        news = [f"{dt:%m-%d %H:%M} {t[:120]}" for dt, t in items[:8]]
        return news or ["近24小时暂无明显重要快讯"]
    except Exception:
        return ["国内新闻 暂未获取"]


def _fetch_a_share():
    import akshare as ak
    today = datetime.date.today()
    lines = []
    em_syms = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指", "sh000688": "科创50"}
    for sym, name in em_syms.items():
        try:
            df = ak.stock_zh_index_daily_em(symbol=sym, start_date=(today - datetime.timedelta(days=5)).strftime("%Y%m%d"), end_date=today.strftime("%Y%m%d"))
            df = df.dropna(subset=["pctChg"])
            if not df.empty:
                pct = df.iloc[-1]["pctChg"]
                lines.append(f"{name} {pct:+.2f}%（来源：东财）")
                continue
        except Exception:
            pass
        try:
            df = ak.stock_zh_index_daily(symbol=sym)
            df = df[df["date"].astype(str) < today.strftime("%Y-%m-%d")]
            prev, last = df.iloc[-2], df.iloc[-1]
            pct = (last["close"] - prev["close"]) / prev["close"] * 100
            lines.append(f"{name} {pct:+.2f}%（来源：新浪）")
        except Exception:
            lines.append(f"{name} 暂未获取")
    try:
        spot = ak.stock_zh_a_spot_em()
        amount = spot["成交额"].sum() / 1e8
        lines.append(f"两市成交额约 {amount:.0f} 亿（来源：东财）")
    except Exception:
        try:
            spot = ak.stock_zh_a_spot()
            amount = spot["成交额"].sum() / 1e8
            lines.append(f"两市成交额约 {amount:.0f} 亿（来源：新浪）")
        except Exception:
            try:
                d = _sina_hq(["s_sh000001", "s_sz399106"])
                amount = (float(d["s_sh000001"][5]) + float(d["s_sz399106"][5])) / 1e4
                lines.append(f"两市成交额约 {amount:.0f} 亿（来源：新浪实时）")
            except Exception:
                lines.append("两市成交额 暂未获取")
    try:
        board = ak.stock_board_industry_spot_em().sort_values("涨跌幅", ascending=False)
        lines.append("领涨行业：" + "、".join(board.head(5)["板块名称"]))
        lines.append("领跌行业：" + "、".join(board.tail(5)["板块名称"]))
    except Exception:
        try:
            sec = ak.stock_sector_spot(indicator="新浪行业").sort_values("涨跌幅", ascending=False)
            lines.append("领涨行业：" + "、".join(sec.head(5)["板块"]))
            lines.append("领跌行业：" + "、".join(sec.tail(5)["板块"]))
        except Exception:
            lines.append("行业板块 暂未获取")
    try:
        em = ak.fund_etf_spot_em()
        for code, name in [("510050", "50ETF"), ("510300", "300ETF"), ("159915", "创业板ETF")]:
            r = em[em["代码"] == code].iloc[0]
            lines.append(f"{name} 份额 {r['最新份额']/1e8:.2f}亿 涨跌 {r['涨跌幅']}%")
    except Exception:
        lines.append("ETF份额 暂未获取")
    return lines


def _fetch_keyword_news(hours: int, keywords: list[str], limit: int, max_pages: int = 20) -> list[str]:
    items = _sina_7x24(hours=hours, max_pages=max_pages)
    return [t[:80] for _, t in items if any(k in t for k in keywords)][:limit]


def _fetch_coal_from_db() -> list[str]:
    """动力煤价格取自煤炭价格页维护的数据库数据（CCI5500 日度、长协价月度）。"""
    from src.modules.market import coal_ltc_service, coal_price_service
    from src.platform.persistence.database import SessionLocal

    coal: list[str] = []
    db = SessionLocal()
    try:
        try:
            prices = coal_price_service.load_price_rows(db)
        except Exception:
            logger.exception("晨报读取 CCI5500 失败")
            prices = {}
        if prices:
            days = sorted(prices)
            last_day = days[-1]
            last = prices[last_day]
            change = ""
            if len(days) >= 2 and prices[days[-2]]:
                pct = (last - prices[days[-2]]) / prices[days[-2]] * 100
                change = f"，较前值 {pct:+.2f}%"
            coal.append(f"CCI5500 动力煤 {last:.0f}元/吨（{last_day}{change}，来源：本地数据库）")
        else:
            coal.append("CCI5500 动力煤：暂未获取")

        try:
            points = coal_ltc_service.load_points(db)
        except Exception:
            logger.exception("晨报读取动力煤长协价失败")
            points = []
        if points:
            latest = points[-1]
            parts = [f"5500大卡 {latest['price_5500']:.0f}元/吨"]
            if latest["price_5000"] is not None:
                parts.append(f"5000大卡 {latest['price_5000']:.0f}元/吨")
            if latest["price_4500"] is not None:
                parts.append(f"4500大卡 {latest['price_4500']:.0f}元/吨")
            coal.append(f"长协价 {latest['month']}：" + "，".join(parts))
        else:
            coal.append("动力煤长协价：暂未获取")
    finally:
        db.close()
    return coal


def _fetch_commodity_industry():
    coal, alu = _fetch_coal_from_db(), []
    # 矿难与行业政策 — 从新闻快讯搜关键词
    try:
        hits = _fetch_keyword_news(48, ["矿难", "煤矿事故", "煤炭", "动力煤", "煤价", "安监", "煤炭行业政策"], 3)
        coal.append("煤炭行业动态：" + "；".join(hits) if hits else "煤炭行业动态：近48小时暂无重大矿难或政策变化")
    except Exception:
        coal.append("煤炭行业动态：暂未获取")
    # 电解铝 — 沪铝主力价格
    try:
        d = _sina_hq(["nf_AL0"])
        f = d.get("nf_AL0")
        if f:
            pct = (float(f[7]) - float(f[4])) / float(f[4]) * 100 if f[7] and f[4] else 0
            alu.append(f"沪铝主力 {f[7]} ({pct:+.2f}%)")
        else:
            alu.append("沪铝：暂未获取")
    except Exception:
        alu.append("沪铝：暂未获取")
    try:
        import akshare as ak
        inv = ak.futures_inventory_em(symbol="沪铝")
        r = inv.iloc[-1]
        alu.append(f"社会库存 {r['库存']} 吨，较前日 {r['增减']:+} 吨")
    except Exception:
        alu.append("沪铝社会库存：暂未获取")
    # 铝行业监管风向 — 搜新闻
    try:
        hits = _fetch_keyword_news(72, ["电解铝", "铝", "铝价", "铝业", "氧化铝", "有色行业", "有色金属"], 2)
        alu.append("铝行业动态：" + "；".join(hits) if hits else "铝行业动态：近72小时暂无重大政策变化")
    except Exception:
        alu.append("铝行业动态：暂未获取")
    return coal, alu


class MorningReportAgent(BaseAgent):
    """每日晨报 Agent（全市场盘前晨报，Bark 直连推送）"""

    name = "morning_report"
    display_name = "每日晨报"
    description = "盘前抓取隔夜全球市场、快讯与煤炭/电解铝产业动态，AI 生成晨报并推送 Bark"

    async def collect(self, context: AgentContext) -> dict:
        if not is_trading_day("CN"):
            return {"skipped": True, "reason": "今日非 A 股交易日"}
        (us, tech, cn), comm, fx, nikkei, news, ashare, (coal, alu) = await asyncio.gather(
            asyncio.to_thread(_fetch_us_market),
            asyncio.to_thread(_fetch_commodities),
            asyncio.to_thread(_fetch_fx),
            asyncio.to_thread(_fetch_nikkei),
            asyncio.to_thread(_fetch_news),
            asyncio.to_thread(_fetch_a_share),
            asyncio.to_thread(_fetch_commodity_industry),
        )
        return {
            "us": us, "tech": tech, "cn": cn, "comm": comm, "fx": fx,
            "nikkei": nikkei, "news": news, "ashare": ashare, "coal": coal, "alu": alu,
            "timestamp": datetime.datetime.now().isoformat(),
        }

    def build_prompt(self, data: dict, context: AgentContext) -> tuple[str, str]:
        system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        user_content = "\n".join([
            "== 隔夜美股（新浪行情）==", *data["us"],
            "重点科技股：" + "；".join(data["tech"]),
            "中概股：" + "；".join(data["cn"]),
            "== 商品（新浪行情）== 纽约黄金/纽约原油/美铜/伦敦金现货：" + "；".join(data["comm"]),
            "== 汇率与利率 ==", *data["fx"],
            "== 亚太早盘 ==", data["nikkei"],
            "== 国内快讯（新浪7x24，近24h）==", *data["news"],
            "== A股昨日（新浪）==", *data["ashare"],
            "== 动力煤 ==", *data["coal"],
            "== 电解铝 ==", *data["alu"],
        ])
        return system_prompt, "真实数据如下：\n" + user_content

    async def analyze(self, context: AgentContext, data: dict) -> AnalysisResult:
        if data.get("skipped"):
            return AnalysisResult(
                agent_name=self.name,
                title=f"【{self.display_name}】{datetime.date.today():%m月%d日}",
                content="今日非 A 股交易日，晨报跳过。",
                raw_data={"skipped": True},
            )
        system_prompt, user_content = self.build_prompt(data, context)
        system_prompt = self.apply_report_language(context, system_prompt)
        report = (await context.ai_client.chat(system_prompt, user_content)).strip()
        if "\n\n" in report:
            subtitle, body = report.split("\n\n", 1)
        else:
            subtitle, body = "", report
        today = datetime.date.today()
        weekday = "一二三四五六日"[today.weekday()]
        title = f"【A股晨报】{today:%m月%d日} 星期{weekday}"
        result = AnalysisResult(
            agent_name=self.name,
            title=title,
            content=report,
            notify_content=body,
            raw_data={**data, "subtitle": subtitle},
        )
        await asyncio.to_thread(
            save_analysis,
            agent_name=self.name,
            stock_symbol="*",
            content=report,
            title=title,
            raw_data={
                "timestamp": data.get("timestamp"),
                "subtitle": subtitle,
                "prompt_context": user_content[:12000],
            },
        )
        return result

    async def should_notify(self, result: AnalysisResult) -> bool:
        return not result.raw_data.get("skipped")

    async def send_notification(self, context: AgentContext, result: AnalysisResult) -> dict:
        channel = await asyncio.to_thread(resolve_bark_channel, self.name)
        if channel is None:
            return {"success": False, "skipped": "no_bark_channel"}
        config = channel.config or {}
        base_url = (context.config.settings.public_base_url or "").rstrip("/")
        return await bark.send(
            config.get("device_key", ""),
            result.title,
            result.notify_content or result.content,
            server=(config.get("server_url") or "").strip() or bark.DEFAULT_SERVER,
            subtitle=result.raw_data.get("subtitle", ""),
            url=f"{base_url}/history" if base_url else "",
            group="晨报",
        )