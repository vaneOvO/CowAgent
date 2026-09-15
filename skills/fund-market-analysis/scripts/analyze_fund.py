#!/usr/bin/env python3
"""Analyze Chinese public fund net-value history from public Eastmoney endpoints."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import socket
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from realtime_estimate import EstimateResult, get_realtime_estimate, prefetch_estimates, run_selftest

logging.basicConfig(
    format="%(levelname)s [%(name)s] %(message)s",
    level=logging.ERROR,
    stream=sys.stderr,
)

log = logging.getLogger("fund_analyze")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
FUND_NAV_URL = (
    "https://fundf10.eastmoney.com/F10DataApi.aspx"
    "?type=lsjz&code={code}&page=1&per={page_size}"
)

# 基金目录（代码↔名称↔类型）是静态元数据，不是实时行情；它体积大(数MB)、几乎不变，
# 仍做本地缓存（默认 6 小时）。注意：这只缓存名称目录，不影响估值/净值/行情的实时性——
# 那些数据每次都实时拉取、绝不缓存。
CATALOG_CACHE_PATH = os.path.expanduser("~/.cache/fund_catalog.json")
CATALOG_TTL = 3600 * 6

# 净值历史也是慢变数据（每天收盘后才更新一次），缓存 4 小时。不影响盘中估值实时性。
NAV_CACHE_PATH = os.path.expanduser("~/.cache/fund_nav.json")
NAV_CACHE_TTL = 3600 * 4
_nav_cache: dict[str, dict[str, Any]] = {}
_nav_cache_loaded = False
_nav_lock = threading.Lock()

DEFAULT_FUND_CODES = [
    "011730",
    "018500",
    "000386",
    "011370",
    "011120",
    "016874",
    "018957",
    "012922",
    "025857",
    "017731",
    "016665",
    "018147",
    "021662",
]


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._in_cell = False
        self._cell_data: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell_data = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr":
            if self._current_row:
                self.rows.append(self._current_row)
                self._current_row = []
        elif tag in ("td", "th"):
            self._in_cell = False
            text = " ".join("".join(self._cell_data).split())
            self._current_row.append(text)

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_data.append(data)


def fetch_text(
    url: str,
    *,
    encoding: str = "utf-8",
    retries: int = 3,
    timeout: float = 15.0,  
    referer: str | None = None,
) -> str:
    last_exc: Exception | None = None
    current_url = url
    current_referer = referer

    for attempt in range(1, retries + 1):
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
            if current_referer:
                headers["Referer"] = current_referer

            request = Request(current_url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
            return raw.decode(encoding, errors="replace")

        except Exception as exc:
            last_exc = exc
            err_str = str(exc).lower()

            if current_url.startswith("https://") and (
                "ssl" in err_str 
                or "handshake" in err_str 
                or "timeout" in err_str 
                or isinstance(exc, socket.timeout)
            ):
                current_url = current_url.replace("https://", "http://", 1)
                if current_referer and current_referer.startswith("https://"):
                    current_referer = current_referer.replace("https://", "http://", 1)
                
                if attempt < retries:
                    log.info("HTTPS握手超时，立即降级HTTP重试: %s", current_url)
                    continue  

            if attempt >= retries:
                break

            sleep_s = min(4.0, 1.0 * (2 ** (attempt - 1))) + random.uniform(0, 1.0)
            log.warning(
                "请求失败，第 %d/%d 次重试，%.2fs 后再试: %s (%s)",
                attempt,
                retries,
                sleep_s,
                current_url,
                exc,
            )
            time.sleep(sleep_s)
            
    raise RuntimeError(f"请求失败（重试{retries}次）: {url}；原因: {last_exc}")


def parse_float(value: str) -> float | None:
    cleaned = value.strip().replace(",", "").replace("%", "").replace("\xa0", "")
    if not cleaned or cleaned in {"--", "-"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fetch_fund_catalog_from_network() -> list[dict[str, str]]:
    text = fetch_text(FUND_LIST_URL, encoding="utf-8")
    result = None
    
    start_idx = text.find("[")
    end_idx = text.rfind("]") + 1
    if start_idx != -1 and end_idx > start_idx:
        try:
            data = json.loads(text[start_idx:end_idx])
            result = [
                {
                    "code": item[0],
                    "pinyin": item[1],
                    "name": item[2],
                    "type": item[3],
                    "pinyin_full": item[4],
                }
                for item in data
            ]
        except Exception as e:
            log.debug("JSON解析基金列表失败，回退到正则: %s", e)

    if result is None:
        matches = re.findall(r'\["(\d{6})","([^"]*)","([^"]*)","([^"]*)","([^"]*)"\]', text)
        result = [
            {
                "code": code,
                "pinyin": pinyin,
                "name": name,
                "type": fund_type,
                "pinyin_full": pinyin_full,
            }
            for code, pinyin, name, fund_type, pinyin_full in matches
        ]
    return result


def load_fund_catalog() -> list[dict[str, str]]:
    # 仅静态目录做缓存（见上方说明）；过期或缺失才重新拉取
    if os.path.exists(CATALOG_CACHE_PATH):
        if time.time() - os.path.getmtime(CATALOG_CACHE_PATH) < CATALOG_TTL:
            try:
                with open(CATALOG_CACHE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log.debug("读取基金目录缓存失败: %s", e)

    catalog = _fetch_fund_catalog_from_network()
    try:
        os.makedirs(os.path.dirname(CATALOG_CACHE_PATH), exist_ok=True)
        with open(CATALOG_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False)
    except Exception as e:
        log.debug("写入基金目录缓存失败: %s", e)
    return catalog


def search_funds(keyword: str, catalog: list[dict[str, str]], limit: int = 20) -> list[dict[str, str]]:
    needle = keyword.strip().lower()
    if not needle:
        return []

    scored: list[tuple[int, dict[str, str]]] = []
    for item in catalog:
        code = item["code"].lower()
        name = item["name"].lower()
        pinyin = item["pinyin"].lower()
        pinyin_full = item["pinyin_full"].lower()

        score = 0
        if needle == code:
            score = 100
        elif code.startswith(needle):
            score = 90
        elif needle in name:
            score = 80
        elif needle in pinyin or needle in pinyin_full:
            score = 60

        if score:
            scored.append((score, item))
            
    scored.sort(key=lambda pair: (-pair[0], pair[1]["code"]))
    return [item for _, item in scored[:limit]]


def _load_nav_cache() -> None:
    global _nav_cache_loaded
    if _nav_cache_loaded: return
    with _nav_lock:
        if _nav_cache_loaded: return
        try:
            if os.path.exists(NAV_CACHE_PATH):
                with open(NAV_CACHE_PATH, "r", encoding="utf-8") as f:
                    _nav_cache.update(json.load(f))
        except Exception as e:
            log.debug("读取净值缓存失败: %s", e)
        _nav_cache_loaded = True


def _persist_nav_cache_locked() -> None:
    # 调用方须已持有 _nav_lock；用临时文件 + 原子替换，避免多线程并发写损坏缓存
    try:
        os.makedirs(os.path.dirname(NAV_CACHE_PATH), exist_ok=True)
        tmp = NAV_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_nav_cache, f, ensure_ascii=False)
        os.replace(tmp, NAV_CACHE_PATH)
    except Exception as e:
        log.debug("写入净值缓存失败: %s", e)


def load_nav_history(code: str, page_size: int) -> list[dict[str, Any]]:
    # 净值历史是慢变数据（每天收盘后才更新一次），缓存 NAV_CACHE_TTL；命中则不重复请求东财。
    _load_nav_cache()
    now = time.time()
    with _nav_lock:
        entry = _nav_cache.get(code)
        if entry and now - entry.get("ts", 0) < NAV_CACHE_TTL and entry.get("page_size", 0) >= page_size:
            return entry["rows"]

    rows = _fetch_nav_history(code, page_size)   # 网络请求放锁外
    with _nav_lock:
        _nav_cache[code] = {"ts": now, "page_size": page_size, "rows": rows}
        _persist_nav_cache_locked()
    return rows


def _fetch_nav_history(code: str, page_size: int) -> list[dict[str, Any]]:
    base_url = FUND_NAV_URL.format(code=quote(code), page_size=page_size)
    url = f"{base_url}&_={int(time.time() * 1000)}"
    referer = f"https://fundf10.eastmoney.com/jjjz_{code}.html"

    text = fetch_text(url, encoding="utf-8", referer=referer)
    match = re.search(r'content:"(.*)",records:', text, flags=re.S)
    if not match:
        raise RuntimeError(f"基金 {code} 的净值接口返回格式无法识别")

    html = match.group(1).replace(r'\"', '"').replace(r"\/", "/")
    parser = TableParser()
    parser.feed(html)
    parser.close()

    rows: list[dict[str, Any]] = []
    for row in parser.rows:
        if len(row) < 4 or not re.match(r"\d{4}-\d{2}-\d{2}", row[0]):
            continue

        unit_nav = parse_float(row[1])
        cumulative_nav = parse_float(row[2])
        daily_growth = parse_float(row[3])

        if unit_nav is None:
            continue

        rows.append(
            {
                "date": row[0],
                "unit_nav": unit_nav,
                "cumulative_nav": cumulative_nav,
                "daily_growth_pct": daily_growth,
            }
        )
    rows.sort(key=lambda item: item["date"])
    if not rows:
        raise RuntimeError(f"基金 {code} 没有可用净值数据")
    return rows


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = value / peak - 1 if peak else 0.0
        worst = min(worst, drawdown)
    return worst


def classify_risk(volatility: float, drawdown: float) -> str:
    if volatility >= 0.25 or drawdown <= -0.25:
        return "高"
    if volatility >= 0.12 or drawdown <= -0.12:
        return "中"
    return "低"


def analyze_history(
    info: dict[str, str],
    history: list[dict[str, Any]],
    days: int,
    estimate: EstimateResult | None = None,
) -> dict[str, Any]:
    
    realtime_est_pct = estimate.estimate_pct if estimate else None
    realtime_est_nav = estimate.estimate_nav if estimate else None
    realtime_est_source = estimate.source if estimate else "failed"
    realtime_est_coverage = estimate.coverage if estimate else 0.0
    realtime_est_message = estimate.message if estimate else None
    realtime_est_time = estimate.estimate_time if estimate else None

    if not history or len(history) < 2:
        if realtime_est_message == "⚠️ 非实时，显示上一交易日涨跌":
            realtime_est_message = "历史缺失，无盘中数据"
        elif not realtime_est_message:
            realtime_est_message = "历史缺失"
            
        return {
            "code": info["code"],
            "name": info["name"],
            "type": info.get("type", "未知"),
            "start_date": "--",
            "end_date": "--",
            "latest_nav": None,
            "latest_nav_date": "--",
            "latest_daily_growth_pct": None,
            "realtime_est_pct": realtime_est_pct,
            "realtime_est_nav": realtime_est_nav,
            "realtime_est_source": realtime_est_source,
            "realtime_est_coverage": realtime_est_coverage,
            "realtime_est_message": realtime_est_message,
            "realtime_est_time": realtime_est_time,
            "total_return_pct": None,
            "annualized_return_pct": None,
            "annualized_volatility_pct": None,
            "max_drawdown_pct": None,
            "risk_level": "未知",
        }

    sample = history[-days:] if days > 0 else history
    navs = [row["unit_nav"] for row in sample]
    returns = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs)) if navs[i - 1] != 0]

    total_return = navs[-1] / navs[0] - 1
    annualized_return = (1 + total_return) ** (252 / max(len(sample) - 1, 1)) - 1
    volatility = statistics.stdev(returns) * math.sqrt(252) if len(returns) >= 2 else 0.0
    drawdown = max_drawdown(navs)

    return {
        "code": info["code"],
        "name": info["name"],
        "type": info.get("type", "未知"),
        "start_date": sample[0]["date"],
        "end_date": sample[-1]["date"],
        "latest_nav": navs[-1],
        "latest_nav_date": sample[-1]["date"],
        "latest_daily_growth_pct": sample[-1].get("daily_growth_pct"),
        "realtime_est_pct": realtime_est_pct,
        "realtime_est_nav": realtime_est_nav,
        "realtime_est_source": realtime_est_source,
        "realtime_est_coverage": realtime_est_coverage,
        "realtime_est_message": realtime_est_message,
        "realtime_est_time": realtime_est_time,
        "total_return_pct": total_return * 100,
        "annualized_return_pct": annualized_return * 100,
        "annualized_volatility_pct": volatility * 100,
        "max_drawdown_pct": drawdown * 100,
        "risk_level": classify_risk(volatility, drawdown),
    }


def pct(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "--"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.2f}%"


def number(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.4f}"


def get_color_str(value: float | None) -> str:
    if value is None:
        return "--"
    if value > 0:
        return f"🔴 +{value:.2f}%"
    if value < 0:
        return f"🟢 {value:.2f}%"
    return "⚫ 0.00%"


_SOURCE_TAG = {
    "eastmoney_gz": "",
    "holding_calc": "",  
    "latest_nav": "",
    "failed": "",
}


def _format_estimate_display(item: dict[str, Any]) -> str:
    est_pct = item.get("realtime_est_pct")
    source = item.get("realtime_est_source", "failed")
    message = item.get("realtime_est_message")

    if message == "⚠️ 非实时，显示上一交易日涨跌":
        message = "非实时"
    elif message and message.startswith("持仓覆盖 "):
        message = message.replace("持仓覆盖 ", "覆盖")
    elif message and "ETF联接→" in message:
        message = "ETF穿透"

    color_str = get_color_str(est_pct)
    tag = _SOURCE_TAG.get(source, "")

    text = f"{tag}{color_str}" if tag else color_str
    if message:
        text += f"（{message}）"
    return text


# ======= 核心修改：树状列表渲染引擎 =======
def render_today(analyses: list[dict[str, Any]], header: bool = True, legend: bool = True) -> str:
    # header/legend 可关闭，便于嵌入「市场数据简报」等更大的输出中
    if not analyses:
        return "暂无基金数据。"

    code_order = {code: idx for idx, code in enumerate(DEFAULT_FUND_CODES)}
    ordered = sorted(analyses, key=lambda item: code_order.get(item["code"], len(code_order)))

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in ordered:
        fund_type = item.get("type") or "其他类型"
        grouped.setdefault(fund_type, []).append(item)

    lines: list[str] = []
    if header:
        lines += [f"📊 基金盘中快报 {datetime.now().strftime('%m-%d %H:%M')}", ""]

    for fund_type, items in grouped.items():
        lines.append(f"【{fund_type}】")
        for i, item in enumerate(items):
            # 判断是否是该类别下的最后一只基金
            is_last_fund = (i == len(items) - 1)
            
            fund_prefix = "└── " if is_last_fund else "├── "
            detail_indent = "    " if is_last_fund else "│   "
            
            lines.append(f"{fund_prefix}{item['name']} ({item['code']})")
            lines.append(f"{detail_indent}├── 当日估值：{_format_estimate_display(item)}")
            lines.append(f"{detail_indent}└── 昨日涨幅：{get_color_str(item.get('latest_daily_growth_pct'))}")
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    if legend:
        lines.extend([
            "",
            "图例：",
            "🟢 下降（负值）",
            "🔴 上升（正值）",
            "“非实时” 表示使用的是上一交易日的涨跌数据",
            "“QDII估值” 为东财官方估值，反映上一海外交易日收盘",
            "“持仓测算” 为按公开持仓+实时行情粗算，覆盖率越低越粗略，仅供参考",
        ])
    return "\n".join(lines)
# ========================================

def render_search(results: list[dict[str, str]]) -> str:
    if not results:
        return "未找到匹配基金。"
    lines = ["## 基金搜索结果", "", "| 代码 | 名称 | 类型 |", "|---|---|---|"]
    for item in results:
        lines.append(f"| {item['code']} | {item['name']} | {item['type']} |")
    return "\n".join(lines)


def render_report(
    analyses: list[dict[str, Any]],
    search_results: list[dict[str, str]] | None = None,
) -> str:
    sections: list[str] = []

    if search_results is not None:
        sections.append(render_search(search_results))

    if analyses:
        sections.append("## 基金市场分析报告")
        for item in analyses:
            est_display = _format_estimate_display(item)
            est_nav_str = number(item.get("realtime_est_nav"))
            est_time_str = item.get("realtime_est_time") or "--"

            sections.extend(
                [
                    "",
                    f"### {item['name']}（{item['code']}）",
                    "",
                    f"- 类型：{item['type']}",
                    f"- 盘中估值：{est_display}",
                    f"- 估值净值：{est_nav_str}（{est_time_str}）",
                    f"- 最新日涨跌：{pct(item.get('latest_daily_growth_pct'))}",
                    f"- 最新单位净值：{number(item.get('latest_nav'))}（{item.get('latest_nav_date', '--')}）",
                    f"- 区间收益：{pct(item.get('total_return_pct'))}",
                    f"- 年化收益：{pct(item.get('annualized_return_pct'))}",
                    f"- 年化波动率：{pct(item.get('annualized_volatility_pct'))}",
                    f"- 最大回撤：{pct(item.get('max_drawdown_pct'))}",
                    f"- 风险等级：{item.get('risk_level')}",
                ]
            )
    return "\n".join(sections).strip()


def _refresh_latest_nav_estimate(
    code: str,
    fund_type: str,
    history: list[dict[str, Any]],
    days: int,
) -> tuple[str, EstimateResult]:
    sample = history[-days:] if days > 0 else history
    latest_nav = sample[-1]["unit_nav"]
    daily_growth = sample[-1].get("daily_growth_pct")
    est = get_realtime_estimate(
        code=code,
        fund_type=fund_type,
        latest_nav=latest_nav,
        daily_growth_pct=daily_growth,
    )
    return code, est


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze Chinese public fund market data.")
    parser.add_argument("--codes", help="基金代码，多个代码用英文逗号分隔")
    parser.add_argument("--my-funds", action="store_true", help="查询默认基金列表的当日估值")
    parser.add_argument("--search", help="按名称、拼音或代码搜索基金")
    parser.add_argument("--days", type=int, default=90, help="分析最近 N 个交易日")
    parser.add_argument("--page-size", type=int, default=200, help="历史净值拉取条数")
    parser.add_argument("--workers", type=int, default=32, help="并发请求线程数（默认 32）")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown", help="输出格式")
    parser.add_argument("--selftest", action="store_true", help="探测各行情/估值数据源连通性（排查部署环境）")
    parser.add_argument("--market-report", action="store_true",
                        help="采集大盘指数/资金流/财经快讯+基金估值，输出市场数据简报（供智能体撰写分析报告）")
    parser.add_argument("--news-limit", type=int, default=8, help="市场简报纳入的财经快讯条数（默认 8）")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志到 stderr")
    args = parser.parse_args()

    if args.my_funds:
        args.codes = ",".join(DEFAULT_FUND_CODES)
        args.days = 14

    if not args.codes and not args.search and not args.selftest and not args.market_report:
        parser.error("至少需要提供 --codes、--search、--my-funds、--market-report 或 --selftest")
    if args.days < 2:
        parser.error("--days 必须大于等于 2")
        
    if args.page_size == 200 and args.days < 150:
        args.page_size = int(args.days * 1.4) + 10
    elif args.page_size < args.days:
        args.page_size = args.days
        
    if args.workers < 1:
        parser.error("--workers 必须大于等于 1")

    return args


def build_analyses(
    codes: list[str], days: int, page_size: int, workers_arg: int, search: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, str]] | None]:
    """拉取净值历史 + 盘中估值并产出每只基金的分析结果。返回 (analyses, search_results)。"""
    search_results = None
    analyses: list[dict[str, Any]] = []
    fund_infos: dict[str, dict[str, str]] = {}
    fund_histories: dict[str, list[dict[str, Any]]] = {}
    failed_codes: list[str] = []

    workers = min(workers_arg, max(1, len(codes)) + 1)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        catalog_future = executor.submit(load_fund_catalog)

        nav_futures = {}
        for code in codes:
            nav_futures[executor.submit(load_nav_history, code, page_size)] = code

        catalog = catalog_future.result()
        catalog_dict = {item["code"]: item for item in catalog}

        if search:
            search_results = search_funds(search, catalog)

        for code in codes:
            fund_infos[code] = catalog_dict.get(
                code, {"code": code, "name": "未知基金", "type": "未知", "pinyin": "", "pinyin_full": ""}
            )

        fund_types = {code: fund_infos[code].get("type", "") for code in codes}

        try:
            estimates = prefetch_estimates(codes, fund_types=fund_types) if codes else {}
        except Exception as exc:
            log.warning("批量估值失败，降级为仅历史净值模式: %s", exc)
            estimates = {}

        estimate_futures = {}
        for future in as_completed(nav_futures):
            code = nav_futures[future]
            try:
                history = future.result()
                fund_histories[code] = history

                est = estimates.get(code)
                if est is None or est.source == "latest_nav":
                    est_future = executor.submit(
                        _refresh_latest_nav_estimate, code, fund_types.get(code, ""), history, days,
                    )
                    estimate_futures[est_future] = code
            except Exception as exc:
                failed_codes.append(code)
                fund_histories[code] = []
                log.warning("基金 %s 拉取失败，将仅保留独立盘中估值信息: %s", code, exc)

        for future in as_completed(estimate_futures):
            code = estimate_futures[future]
            try:
                _c, est = future.result()
                estimates[code] = est
            except Exception as exc:
                log.warning("基金 %s 回退估值刷新失败，保留原估值: %s", code, exc)

    if failed_codes:
        log.warning("以下基金历史数据拉取失败（但仍将显示盘中估值）: %s", ",".join(failed_codes))

    for code in codes:
        analyses.append(
            analyze_history(fund_infos[code], fund_histories.get(code, []), days, estimates.get(code))
        )
    return analyses, search_results


def build_portfolio_insight(analyses: list[dict[str, Any]], codes: list[str], workers: int = 16) -> dict[str, Any]:
    """组合透视：底层持仓重合（敞口集中度/行业暴露）+ 类型配置。
    注意：仅知道持有哪些基金、不知各基金的金额占比，故重合权重为「该股在各基金内的占比」，
    不是组合层面的金额权重——这点报告里要诚实说明。"""
    from realtime_estimate import get_holdings

    code_to_name = {a["code"]: a["name"] for a in analyses}
    holdings_by_code: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(codes)))) as ex:
        futs = {ex.submit(get_holdings, c): c for c in codes}
        for fut in as_completed(futs):
            try:
                holdings_by_code[futs[fut]] = fut.result()
            except Exception:
                holdings_by_code[futs[fut]] = []

    agg: dict[str, dict[str, Any]] = {}
    for code in codes:
        for h in holdings_by_code.get(code, []):
            if h.holding_type not in ("stock", "etf"):  # 债券重合意义不大
                continue
            e = agg.setdefault(h.stock_code, {"name": h.stock_name, "funds": []})
            e["funds"].append({"fund": code_to_name.get(code, code), "weight": round(h.weight * 100, 2)})

    overlap = sorted(
        ((sc, e) for sc, e in agg.items() if len(e["funds"]) >= 2),
        key=lambda x: (len(x[1]["funds"]), sum(f["weight"] for f in x[1]["funds"])),
        reverse=True,
    )[:10]

    type_dist: dict[str, int] = {}
    for a in analyses:
        t = a.get("type") or "未知"
        type_dist[t] = type_dist.get(t, 0) + 1

    return {
        "overlap": [{"code": sc, "name": e["name"], "held_by": e["funds"]} for sc, e in overlap],
        "type_dist": type_dist,
    }


def render_portfolio_insight(analyses: list[dict[str, Any]], insight: dict[str, Any]) -> str:
    """把组合透视整理成文本块，供智能体做深度/风险分析。"""
    order = {c: i for i, c in enumerate(DEFAULT_FUND_CODES)}
    ordered = sorted(analyses, key=lambda a: order.get(a["code"], 999))

    lines = ["【组合透视·风险指标】区间收益/年化波动/最大回撤/风险等级"]
    for a in ordered:
        lines.append(
            f"  · {a['name']}：{pct(a.get('total_return_pct'))} / "
            f"{pct(a.get('annualized_volatility_pct'))} / "
            f"{pct(a.get('max_drawdown_pct'))} / {a.get('risk_level', '--')}"
        )
    lines.append("")

    overlap = insight.get("overlap") or []
    if overlap:
        lines.append("【组合透视·底层重合持仓】同一股票被多只基金持有=实际敞口集中（权重=该股在各基金内占比，非组合金额占比）")
        for o in overlap:
            held = " / ".join(f"{f['fund']} {f['weight']}%" for f in o["held_by"])
            lines.append(f"  · {o['name']}({o['code']}) {len(o['held_by'])}只持有：{held}")
        lines.append("")

    td = insight.get("type_dist") or {}
    if td:
        lines.append("【组合透视·类型配置】" + "  ".join(f"{k}×{v}" for k, v in td.items()))
    return "\n".join(lines).rstrip()


def run_market_report(args: argparse.Namespace) -> int:
    """采集市场快照（指数/资金流/快讯）+ 基金估值联动，输出结构化数据供智能体撰写分析报告。"""
    import market_data

    snapshot = market_data.collect_market(news_limit=args.news_limit)

    # 用近 60 日窗口，使区间收益/波动/回撤等风险指标有意义（当日估值仍是实时的）
    fund_analyses: list[dict[str, Any]] = []
    insight: dict[str, Any] = {}
    try:
        fund_analyses, _ = build_analyses(DEFAULT_FUND_CODES, 60, 94, args.workers)
        insight = build_portfolio_insight(fund_analyses, DEFAULT_FUND_CODES, args.workers)
    except Exception as exc:
        log.warning("基金组合透视失败（市场数据照常输出）: %s", exc)

    if args.format == "json":
        print(json.dumps({
            "market": snapshot,
            "funds": fund_analyses,
            "portfolio_insight": insight,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2))
    else:
        fund_block = render_today(fund_analyses, header=False, legend=False) if fund_analyses else None
        text = market_data.render_market_text(snapshot, fund_block)
        if insight:
            text += "\n\n" + render_portfolio_insight(fund_analyses, insight)
        print(text)
    return 0


def main() -> int:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.selftest:
        print(run_selftest())
        return 0

    if args.market_report:
        return run_market_report(args)

    try:
        codes = []
        if args.codes:
            codes = [code.strip() for code in args.codes.split(",") if code.strip()]
            for code in codes:
                if not re.fullmatch(r"\d{6}", code):
                    raise RuntimeError(f"基金代码格式错误：{code}")

        analyses, search_results = build_analyses(
            codes, args.days, args.page_size, args.workers, search=args.search
        )

        output = {
            "search_results": search_results,
            "analyses": analyses,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

        if args.format == "json":
            print(json.dumps(output, ensure_ascii=False, indent=2))
        elif args.my_funds:
            print(render_today(analyses))
        else:
            print(render_report(analyses, search_results))

        return 0

    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as exc:
        log.exception("运行时错误")
        error = {"error": str(exc)}
        if args.format == "json":
            print(json.dumps(error, ensure_ascii=False, indent=2))
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
