#!/usr/bin/env python3
"""市场数据采集：大盘指数 / 主力资金流 / 财经快讯。

输出结构化数据供调用方（智能体）生成分析报告——本模块只负责"采集 + 整理"，不做 AI 撰写。
全部实时拉取、不缓存。各源相互独立且可降级：任一失败仅该部分缺失，不影响其余。
"""
from __future__ import annotations

import json
import logging
import re
from urllib.request import Request, urlopen

from realtime_estimate import StockQuote, _fetch_quotes_tencent  # 复用腾讯行情解析

log = logging.getLogger("market_data")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 大盘指数（腾讯代码, 名称, 地区）。美股在中国白天为隔夜收盘价。
INDICES: list[tuple[str, str, str]] = [
    ("sh000001", "上证指数", "A股"),
    ("sz399001", "深证成指", "A股"),
    ("sz399006", "创业板指", "A股"),
    ("sh000300", "沪深300", "A股"),
    ("hkHSI", "恒生指数", "港股"),
    ("hkHSTECH", "恒生科技", "港股"),
    ("usDJI", "道琼斯", "美股"),
    ("usIXIC", "纳斯达克", "美股"),
    ("usINX", "标普500", "美股"),
]

# 沪深主力资金流（分钟级 K 线，最后一条即当日累计净流入）
EM_FFLOW_URL = (
    "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    "?lmt=0&klt=1&secid=1.000001&secid2=0.399001"
    "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56"
)
# 东财全球财经快讯
EM_NEWS_URL = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"


def _get(url: str, *, referer: str = "https://www.eastmoney.com/", timeout: float = 4.0,
         encoding: str = "utf-8") -> str:
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Accept": "*/*",
    })
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(encoding, errors="replace")


def _color(pct: float | None) -> str:
    """涨跌色：红涨绿跌（A股习惯）。"""
    if pct is None:
        return "--"
    if pct > 0:
        return f"🔴+{pct:.2f}%"
    if pct < 0:
        return f"🟢{pct:.2f}%"
    return "⚫0.00%"


def fetch_indices() -> list[dict]:
    """实时大盘指数。失败返回 []。"""
    try:
        codes = [c for c, _, _ in INDICES]
        quotes: dict[str, StockQuote] = _fetch_quotes_tencent(codes)
    except Exception as exc:
        log.debug("指数行情失败: %s", exc)
        return []

    out: list[dict] = []
    for code, name, region in INDICES:
        q = quotes.get(code[2:])  # _fetch_quotes_tencent 以去前缀的代码为 key
        if not q:
            continue
        pct = q.pct_change * 100.0
        out.append({"name": name, "region": region, "point": q.current,
                    "pct": pct, "display": _color(pct)})
    return out


def fetch_capital_flow() -> dict | None:
    """沪深主力资金净流入（当日累计，单位亿元）。失败返回 None。"""
    try:
        data = json.loads(_get(EM_FFLOW_URL, referer="https://data.eastmoney.com/"))
        klines = data.get("data", {}).get("klines") or []
        if not klines:
            return None
        # f51=时间, f52=主力净流入, f53=小单, f54=中单, f55=大单, f56=超大单（单位：元）
        parts = klines[-1].split(",")
        if len(parts) < 6:
            return None
        yi = 1e8
        return {
            "time": parts[0],
            "main": float(parts[1]) / yi,       # 主力净流入
            "super_big": float(parts[5]) / yi,  # 超大单净流入
            "big": float(parts[4]) / yi,        # 大单净流入
        }
    except Exception as exc:
        log.debug("资金流失败: %s", exc)
        return None


def fetch_news(limit: int = 8) -> list[dict]:
    """东财财经快讯标题（可降级，失败返回 []）。"""
    try:
        text = _get(EM_NEWS_URL, referer="https://kuaixun.eastmoney.com/")
        m = re.search(r"var\s+ajaxResult\s*=\s*(\{.*\})\s*;?\s*$", text, re.S)
        data = json.loads(m.group(1) if m else text)
        items = data.get("LivesList") or []
        out: list[dict] = []
        for it in items[:limit]:
            title = (it.get("title") or it.get("simtitle") or "").strip()
            if not title:
                continue
            when = (it.get("showtime") or it.get("creturl") or "").strip()
            out.append({"title": title, "time": when, "digest": (it.get("digest") or "").strip()})
        return out
    except Exception as exc:
        log.debug("财经快讯失败: %s", exc)
        return []


def collect_market(news_limit: int = 8) -> dict:
    """汇总市场快照（指数 + 资金流 + 快讯）。每项独立降级。"""
    return {
        "indices": fetch_indices(),
        "capital_flow": fetch_capital_flow(),
        "news": fetch_news(news_limit),
    }


def render_market_text(snapshot: dict, fund_block: str | None = None) -> str:
    """把快照整理成可读「市场数据简报」（供智能体撰写报告的输入）。"""
    from datetime import datetime
    lines: list[str] = [f"📊 市场数据简报 {datetime.now().strftime('%m-%d %H:%M')}", ""]

    indices = snapshot.get("indices") or []
    if indices:
        lines.append("【大盘指数】")
        for region in ("A股", "港股", "美股"):
            group = [i for i in indices if i["region"] == region]
            if not group:
                continue
            tag = "（隔夜收盘）" if region == "美股" else ""
            lines.append(f"· {region}{tag}")
            for i in group:
                lines.append(f"  {i['name']} {i['point']:.2f} {i['display']}")
        lines.append("")

    flow = snapshot.get("capital_flow")
    if flow:
        lines.append(f"【沪深主力资金流】截至 {flow['time'][-5:]}")
        lines.append(f"  主力净流入 {flow['main']:+.1f}亿（超大单 {flow['super_big']:+.1f}亿 / 大单 {flow['big']:+.1f}亿）")
        lines.append("")

    news = snapshot.get("news") or []
    if news:
        lines.append("【财经快讯】")
        for n in news:
            t = f"[{n['time']}] " if n.get("time") else ""
            lines.append(f"  · {t}{n['title']}")
        lines.append("")

    if fund_block:
        lines.append("【我的基金估值】")
        lines.append(fund_block)
        lines.append("")

    if not indices and not flow and not news:
        lines.append("（市场数据源暂不可达）")

    return "\n".join(lines).rstrip()
