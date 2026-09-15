#!/usr/bin/env python3
"""
实时估值模块 - 三级降级策略

Level 1: 东财官方估值 API (fundgz.1234567.com.cn)
Level 2: 持仓加权计算 (EastMoney 持仓 + 腾讯行情)
Level 3: 最新净值兜底
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

log = logging.getLogger("fund_estimate")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

EASTMONEY_GZ_URL = "http://fundgz.1234567.com.cn/js/{code}.js?rt={ts}"
EASTMONEY_HOLDING_JSON_URL = (
    "http://fundmobapi.eastmoney.com/FundMApi/FundArchivesDatas.ashx"
    "?type=jjcc&code={code}&topline=15&month=&year=&page=1&per=15"
    "&plat=Android&appType=ttjj&product=EFund&version=1&deviceid=x&Uid=x"
)
EASTMONEY_HOLDING_HTML_URL = (
    "http://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    "?type=jjcc&code={code}&topline=15&year=&month="
)

TENCENT_QUOTE_URL = "http://qt.gtimg.cn/q={codes}"

# Yahoo Finance：覆盖腾讯不支持的台/韩/日股。境外 HTTPS 接口，国内直连可能不稳定，
# 全程可降级（短超时 + 失败跳过 + 连续失败熔断 + FUND_DISABLE_OVERSEA=1 可关闭）。
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=6&newsCount=0"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1mo"

# 缓存策略：只缓存「慢变数据」（持仓=季度披露），不缓存任何实时行情/估值。
# 行情(腾讯/Yahoo)、东财估值每次调用都实时拉取，保证当日估值反映当下行情。
HOLDINGS_CACHE_PATH = os.path.expanduser("~/.cache/fund_holdings.json")
HOLDINGS_CACHE_TTL = 86400        # 持仓 1 天有效期（季度才变，1天足够新鲜且大幅减轻东财负载）
HOLDINGS_CACHE_SCHEMA = 2         # 持仓解析逻辑版本；递增即自动失效旧缓存（含早期只存A股的版本）

_OVERSEA_DISABLED = os.environ.get("FUND_DISABLE_OVERSEA") == "1"
_OVERSEA_TIMEOUT = 3.5          # Yahoo 单次请求超时（短，避免国内直连拖慢整体）
_OVERSEA_FAIL_LIMIT = 3         # 单次运行内连续失败达此数即熔断，跳过剩余境外查询
_oversea_fails = 0

@dataclass
class Holding:
    stock_code: str
    stock_name: str
    market: str
    weight: float
    holding_type: str

    @property
    def full_code(self) -> str:
        return self.market + self.stock_code


@dataclass
class StockQuote:
    code: str
    current: float
    prev_close: float

    @property
    def pct_change(self) -> float:
        if self.prev_close <= 0:
            return 0.0
        return self.current / self.prev_close - 1.0


@dataclass
class EstimateResult:
    code: str
    estimate_pct: float | None
    estimate_nav: float | None
    estimate_time: str | None
    source: str
    coverage: float = 0.0
    message: str | None = None


_holdings_cache: dict[str, tuple[float, list[Holding]]] = {}
_cache_loaded = False
_holdings_lock = threading.Lock()


# ==========================================
# 核心网络引擎：支持系统代理 + 快失败降级机制
# ==========================================
def _fetch_text(url: str, *, encoding: str = "utf-8", retries: int = 3, timeout: float = 3.0) -> str:
    current_url = url
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Referer": "http://fund.eastmoney.com/",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
            req = Request(current_url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return raw.decode(encoding, errors="replace")
            
        except Exception as exc:
            last_exc = exc
            err_str = str(exc).lower()
                    
            if current_url.startswith("https://") and (
                "ssl" in err_str or "timeout" in err_str or "handshake" in err_str or isinstance(exc, socket.timeout)
            ):
                current_url = current_url.replace("https://", "http://", 1)
                if attempt < retries:
                    log.info("HTTPS握手超时，立即降级HTTP重试: %s", current_url)
                    continue
                    
            if attempt >= retries:
                break
            
            sleep_s = min(1.0, 0.2 * (2 ** (attempt - 1))) + random.uniform(0, 0.2)
            time.sleep(sleep_s)
            
    raise RuntimeError(f"请求失败 {url}: {last_exc}")


# ==========================================
# 持仓硬盘缓存（慢变数据；不缓存任何实时行情）
# ==========================================
def _init_disk_cache() -> None:
    global _cache_loaded
    if _cache_loaded: return
    try:
        if os.path.exists(HOLDINGS_CACHE_PATH):
            with open(HOLDINGS_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # schema 版本不匹配（解析逻辑变更）→ 丢弃旧缓存重抓，避免早期只存A股的陈旧数据
            if isinstance(data, dict) and data.get("__schema__") == HOLDINGS_CACHE_SCHEMA:
                now = time.time()
                for code, payload in data.get("funds", {}).items():
                    expire_ts = payload.get("expire_ts", 0)
                    if now < expire_ts:
                        _holdings_cache[code] = (expire_ts, [Holding(**h) for h in payload.get("holdings", [])])
    except Exception as e:
        log.debug(f"加载持仓缓存失败: {e}")
    _cache_loaded = True

def _save_disk_cache() -> None:
    # 临时文件 + 原子替换，并加锁，避免多线程并发抓取持仓时写文件损坏
    try:
        with _holdings_lock:
            os.makedirs(os.path.dirname(HOLDINGS_CACHE_PATH), exist_ok=True)
            dump_data = {
                "__schema__": HOLDINGS_CACHE_SCHEMA,
                "funds": {
                    code: {"expire_ts": expire, "holdings": [asdict(h) for h in items]}
                    for code, (expire, items) in _holdings_cache.items()
                },
            }
            tmp = HOLDINGS_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dump_data, f, ensure_ascii=False)
            os.replace(tmp, HOLDINGS_CACHE_PATH)
    except Exception as e:
        log.debug(f"保存持仓缓存失败: {e}")


# ==========================================
# 基础辅助函数与 HTML 解析器
# ==========================================
def _parse_float(value: str) -> float | None:
    cleaned = value.strip().replace(",", "").replace("%", "").replace("\xa0", "")
    if not cleaned or cleaned in {"--", "-"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

def _is_same_day_time(time_str: str) -> bool:
    if not time_str: return False
    try:
        dt = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M")
        return dt.date() == datetime.now().date()
    except ValueError:
        return False

def _is_recent_time(time_str: str, max_days: int = 7) -> bool:
    """QDII 估值天然滞后（反映上一个海外交易日收盘），放宽到最近 max_days 日内仍视为有效。"""
    if not time_str: return False
    try:
        dt = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    age_days = (datetime.now() - dt).total_seconds() / 86400.0
    return -1.0 <= age_days <= max_days

def _is_suspicious_estimate(fund_type: str, est_pct: float) -> bool:
    abs_pct = abs(est_pct)
    if "货币" in fund_type or "现金" in fund_type: return abs_pct > 0.05
    if "债券型" in fund_type and "二级" not in fund_type and "可转债" not in fund_type: return abs_pct > 0.80
    if "二级" in fund_type or "偏债" in fund_type: return abs_pct > 1.80
    return abs_pct > 9.90

QDII_KEYWORDS = ("QDII", "海外", "全球", "纳斯达克", "标普")

def _is_qdii_type(fund_type: str) -> bool:
    return any(k in fund_type.upper() for k in QDII_KEYWORDS)

def _to_market_prefix(code: str) -> str:
    if code.startswith(("6", "5", "9")): return "sh"
    return "sz"

def _detect_market(code: str) -> str:
    """按代码格式粗略识别市场（仅在拿不到东财市场ID时兜底）。
    A股 6 位数字 → sh/sz；港股 5 位数字 → hk；美股字母代码 → us。
    注意：韩/台/日股也是数字代码，格式法无法区分，故优先用 _mktid_to_prefix。"""
    rc = code.strip().upper()
    if re.fullmatch(r"\d{6}", rc): return _to_market_prefix(rc)
    if re.fullmatch(r"\d{5}", rc): return "hk"
    if re.fullmatch(r"[A-Z]{1,6}(?:\.[A-Z]+)?", rc): return "us"
    return ""

# 东财行情链接 unify/r/<市场ID>.<代码> 中的市场ID → 腾讯行情前缀。
# 仅 A股/港股/美股可经腾讯取价；韩/台/日股东财不提供行情链接，映射缺失即跳过。
_EM_MARKET_ID = {
    "0": "sz", "1": "sh",          # A股
    "105": "us", "106": "us", "107": "us",  # 纳斯达克 / 纽交所 / 美交所
    "116": "hk", "153": "hk", "156": "hk",  # 港股
}

def _mktid_to_prefix(mktid: str) -> str:
    return _EM_MARKET_ID.get(mktid, "")

def _classify_holding_type(code: str) -> str:
    if code.startswith(("510", "511", "512", "513", "515", "516", "517", "518", "159", "16")): return "etf"
    if code.startswith(("10", "11", "12", "01")): return "bond"
    return "stock"

class _SimpleTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._in_cell = False
        self._cell_data: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr": self._current_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell_data = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._current_row:
            self.rows.append(self._current_row)
            self._current_row = []
        elif tag in ("td", "th"):
            self._in_cell = False
            self._current_row.append(" ".join("".join(self._cell_data).split()))

    def handle_data(self, data: str) -> None:
        if self._in_cell: self._cell_data.append(data)


# ==========================================
# 业务逻辑接口
# ==========================================
def _extract_holdings_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("Datas", "data", "datas", "Data"):
        value = data.get(key)
        if isinstance(value, list): return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for sub_key in ("JJCC", "jjcc", "fundStocks", "items", "list"):
                sub_value = value.get(sub_key)
                if isinstance(sub_value, list): return [item for item in sub_value if isinstance(item, dict)]
            for sub_value in value.values():
                if isinstance(sub_value, list) and sub_value and isinstance(sub_value[0], dict):
                    return [item for item in sub_value if isinstance(item, dict)]
    return []

def _fetch_holdings_json_api(code: str) -> list[Holding] | None:
    url = EASTMONEY_HOLDING_JSON_URL.format(code=quote(code))
    try:
        text = _fetch_text(url)
        data = json.loads(text)
        items = _extract_holdings_items(data)
        if not items: return None

        holdings: list[Holding] = []
        for item in items:
            raw_code = str(item.get("GPDM") or item.get("stockCode") or item.get("ZQDM") or item.get("code") or "").strip()
            name = str(item.get("GPJC") or item.get("stockName") or item.get("ZQJC") or item.get("name") or "").strip()
            weight_str = str(item.get("JZBL") or item.get("ratio") or item.get("weight") or item.get("percent") or "").strip()

            market = _detect_market(raw_code)
            if not market: continue
            weight = _parse_float(weight_str)
            if weight is None: continue

            holdings.append(Holding(
                stock_code=raw_code, stock_name=name,
                market=market, weight=weight / 100.0,
                holding_type=_classify_holding_type(raw_code)
            ))
        return holdings if holdings else None
    except Exception as exc:
        log.debug(f"[{code}] 持仓 JSON 接口失败: {exc}")
        return None

def _fetch_holdings_html(code: str) -> list[Holding] | None:
    url = EASTMONEY_HOLDING_HTML_URL.format(code=quote(code))
    try:
        text = _fetch_text(url)
        match = re.search(r'content:"(.*?)",', text, re.S)
        if not match: return None

        html = match.group(1).replace(r"\"", '"').replace(r"\/", "/")
        # 从行情链接 unify/r/<市场ID>.<代码> 提取每只持仓的权威市场归属。
        # 仅可报价证券（A股/港股/美股）才有链接；韩/台/日股无链接，不会进此映射。
        mkt_map = {c: m for m, c in re.findall(r"unify/r/(\d+)\.([A-Za-z0-9]+)", html)}

        parser = _SimpleTableParser()
        parser.feed(html)

        holdings: list[Holding] = []
        for row in parser.rows:
            if len(row) < 3: continue
            # 优先用行情链接里的代码定位代码列（市场可靠）
            code_idx = next((i for i, c in enumerate(row) if c.strip() in mkt_map), -1)
            if code_idx >= 0:
                raw_code = row[code_idx].strip()
                market = _mktid_to_prefix(mkt_map[raw_code])
            elif mkt_map:
                # 本页有行情链接体系，但此行代码无链接 → 东财不报价的境外股（台/韩/日）。
                # 取代码列（序号之后、第一个含数字的代码样式单元格），标记 oversea 交给 Yahoo。
                # 不再格式识别为 A股，避免误匹配同代码境内股。
                code_idx = next((i for i, c in enumerate(row)
                                 if i > 0 and re.fullmatch(r"[A-Za-z0-9.]{4,14}", c.strip())
                                 and any(ch.isdigit() for ch in c)), -1)
                if code_idx < 0: continue
                raw_code = row[code_idx].strip()
                market = "oversea"
            else:
                # 整页都没有行情链接时，退回格式识别（兜底）
                code_idx = next((i for i, c in enumerate(row) if _detect_market(c.strip())), -1)
                if code_idx < 0: continue
                raw_code = row[code_idx].strip()
                market = _detect_market(raw_code)

            if not market: continue
            name = row[code_idx + 1].strip() if code_idx + 1 < len(row) else ""
            weight = next((_parse_float(cell) for cell in row if "%" in cell and _parse_float(cell) is not None), None)
            if weight is None: continue

            holdings.append(Holding(
                stock_code=raw_code, stock_name=name,
                market=market, weight=weight / 100.0,
                holding_type=_classify_holding_type(raw_code)
            ))
        return holdings if holdings else None
    except Exception as exc:
        log.debug(f"[{code}] 持仓 HTML 接口失败: {exc}")
        return None

def get_holdings(code: str) -> list[Holding]:
    # 持仓是慢变数据（季度披露），缓存 1 天；过期/未命中才实时抓取。
    _init_disk_cache()
    now = time.time()
    if code in _holdings_cache:
        expire_ts, cached = _holdings_cache[code]
        if now < expire_ts:
            return cached

    # HTML 接口优先：移动端 JSON 接口(fundmobapi)已长期 404/超时，再做首选只会每只基金白等数秒
    holdings = _fetch_holdings_html(code) or _fetch_holdings_json_api(code) or []
    if holdings:
        _holdings_cache[code] = (now + HOLDINGS_CACHE_TTL, holdings)
        _save_disk_cache()
    return holdings


# ==========================================
# 行情计算与批量接口
# ==========================================
def _parse_tencent_response(text: str) -> dict[str, StockQuote]:
    result: dict[str, StockQuote] = {}
    for line in text.strip().split("\n"):
        match = re.match(r'v_(\w+)="([^"]*)"', line.strip())
        if not match: continue
        
        raw_code = match.group(1)[2:]
        parts = match.group(2).split("~")
        if len(parts) < 5: continue

        current, prev_close = _parse_float(parts[3]), _parse_float(parts[4])
        if current and prev_close and current > 0 and prev_close > 0:
            result[raw_code] = StockQuote(code=raw_code, current=current, prev_close=prev_close)
    return result

def _fetch_quotes_tencent(full_codes: list[str]) -> dict[str, StockQuote]:
    if not full_codes: return {}
    try:
        text = _fetch_text(TENCENT_QUOTE_URL.format(codes=",".join(full_codes)), encoding="gbk")
        return _parse_tencent_response(text)
    except Exception as exc:
        log.warning(f"腾讯行情请求失败: {exc}")
        return {}

def batch_stock_quotes(raw_codes: list[str]) -> dict[str, StockQuote]:
    if not raw_codes: return {}
    unique_full = list(dict.fromkeys(_to_market_prefix(rc) + rc for rc in raw_codes))
    result: dict[str, StockQuote] = {}
    CHUNK_SIZE = 50
    for i in range(0, len(unique_full), CHUNK_SIZE):
        result.update(_fetch_quotes_tencent(unique_full[i:i + CHUNK_SIZE]))
    return result

def get_etf_realtime_quote(etf_code: str) -> StockQuote | None:
    return batch_stock_quotes([etf_code]).get(etf_code)


# ==========================================
# Yahoo Finance：台/韩/日股取价（境外接口，全程可降级）
# ==========================================
# 同一次运行内，已解析过的「东财代码 → Yahoo 符号」记一下，避免对同一只股重复搜索
# （这只是代码→ticker 的稳定映射，不是行情；进程结束即清空，不跨请求持久化）。
_oversea_symbols: dict[str, str] = {}

# 带交易所后缀的东财代码，可直接拼出 Yahoo 符号，无需搜索
_OVERSEA_SUFFIX = {"KS": ".KS", "KQ": ".KQ", "JP": ".T", "TW": ".TW", "TT": ".TW"}

def _oversea_available() -> bool:
    return not _OVERSEA_DISABLED and _oversea_fails < _OVERSEA_FAIL_LIMIT

def _note_oversea_result(ok: bool) -> None:
    global _oversea_fails
    _oversea_fails = 0 if ok else _oversea_fails + 1

def _fetch_https_json(url: str, timeout: float = _OVERSEA_TIMEOUT) -> Any:
    """短超时 HTTPS 拉取并解析 JSON；不降级到 HTTP（Yahoo 仅支持 HTTPS）。失败抛异常。"""
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))

def _resolve_yahoo_symbol(code: str) -> str | None:
    """东财境外代码 → Yahoo 符号。带后缀的直接拼；其余用 Yahoo 搜索取首条股票结果。"""
    rc = code.strip().upper()
    m = re.fullmatch(r"(\d+)(KS|KQ|JP|TW|TT)", rc)
    if m:
        return m.group(1) + _OVERSEA_SUFFIX[m.group(2)]

    if rc in _oversea_symbols:           # 本次运行内已解析过
        return _oversea_symbols[rc] or None
    if not _oversea_available():
        return None

    query = re.sub(r"(KS|KQ|JP|TW|TT)$", "", rc)  # 去掉可能的后缀再搜
    try:
        data = _fetch_https_json(YAHOO_SEARCH_URL.format(q=quote(query)))
        _note_oversea_result(True)
    except Exception as exc:
        _note_oversea_result(False)
        log.debug(f"[{code}] Yahoo 搜索失败: {exc}")
        return None

    quotes = [q for q in data.get("quotes", []) if isinstance(q, dict)]
    sym = next((q.get("symbol") for q in quotes
                if q.get("quoteType") == "EQUITY" and q.get("symbol")
                and not str(q.get("symbol")).endswith("-USD")), None)
    if not sym and quotes:
        sym = quotes[0].get("symbol")
    _oversea_symbols[rc] = sym or ""     # 本次运行内记忆（含负结果），不持久化
    return sym or None

def _yahoo_quote(code: str) -> StockQuote | None:
    """取单只境外股的单日涨跌，构造 StockQuote（涨跌幅与币种无关）。失败返回 None。
    用 close 数组的最后两个有效收盘算单日涨跌——meta.chartPreviousClose 是整段区间起点前的收盘
    （多日涨幅），不能用；meta.previousClose 常缺失。"""
    if not _oversea_available():
        return None
    symbol = _resolve_yahoo_symbol(code)
    if not symbol:
        return None
    try:
        data = _fetch_https_json(YAHOO_CHART_URL.format(sym=quote(symbol)))
        res = data["chart"]["result"][0]
        meta = res.get("meta", {})
        closes = [c for c in res["indicators"]["quote"][0].get("close", []) if c is not None]
        _note_oversea_result(True)
        if len(closes) >= 2:
            current = meta.get("regularMarketPrice") or closes[-1]
            prev = closes[-2]
            if current and prev and float(prev) > 0:
                return StockQuote(code=code, current=float(current), prev_close=float(prev))
    except Exception as exc:
        _note_oversea_result(False)
        log.debug(f"[{code}] Yahoo 取价失败({symbol}): {exc}")
    return None

def _quotes_for_oversea(holdings: list[Holding]) -> dict[str, StockQuote]:
    """并发实时取境外持仓行情（Yahoo）；任一失败仅跳过该只，不影响其余。"""
    if not holdings or not _oversea_available():
        return {}
    uniq = list({h.stock_code: h for h in holdings}.values())
    result: dict[str, StockQuote] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(uniq))) as ex:
        futs = {ex.submit(_yahoo_quote, h.stock_code): h for h in uniq}
        for fut in as_completed(futs):
            h = futs[fut]
            try:
                sq = fut.result()
            except Exception:
                sq = None
            if sq:
                result[h.stock_code] = sq
    return result

_TENCENT_MARKETS = {"sh", "sz", "us", "hk"}

def _quotes_for_holdings(holdings: list[Holding]) -> dict[str, StockQuote]:
    """按持仓所属市场批量取价，返回 {stock_code: quote}。
    A股/港股/美股走腾讯（域内、快）；台/韩/日股(market='oversea')走 Yahoo（可降级）。
    每次实时拉取，不做缓存。"""
    if not holdings: return {}
    result: dict[str, StockQuote] = {}

    tencent_holdings = [h for h in holdings if h.market in _TENCENT_MARKETS]
    oversea_holdings = [h for h in holdings if h.market == "oversea"]

    uniq_full = list(dict.fromkeys(h.full_code for h in tencent_holdings))
    CHUNK_SIZE = 50
    for i in range(0, len(uniq_full), CHUNK_SIZE):
        result.update(_fetch_quotes_tencent(uniq_full[i:i + CHUNK_SIZE]))

    # 境外股（Yahoo）：失败仅跳过，不影响腾讯部分
    if oversea_holdings:
        result.update(_quotes_for_oversea(oversea_holdings))
    return result

def detect_fund_subtype(fund_type: str, holdings: list[Holding]) -> str:
    if "货币" in fund_type or "现金" in fund_type: return "money"
    if "债" in fund_type and "股" not in fund_type: return "bond"
    if holdings:
        etf_holdings = [h for h in holdings if h.holding_type == "etf"]
        if etf_holdings and max(etf_holdings, key=lambda h: h.weight).weight >= 0.80:
            return "etf_linked"
    if _is_qdii_type(fund_type):
        return "qdii"
    return "stock"

def _try_eastmoney_gz_api(code: str, fund_type: str = "") -> EstimateResult | None:
    ts = int(time.time() * 1000)
    url = EASTMONEY_GZ_URL.format(code=code, ts=ts)
    try:
        text = _fetch_text(url)
        match = re.search(r"jsonpgz\((\{.*?\})\)", text, re.S)
        if not match: return None

        data: dict[str, Any] = json.loads(match.group(1))
        gztime = str(data.get("gztime") or "").strip()
        gsz, dwjz, gszzl = _parse_float(data.get("gsz")), _parse_float(data.get("dwjz")), _parse_float(data.get("gszzl"))

        same_day = _is_same_day_time(gztime)
        is_qdii = _is_qdii_type(fund_type)
        # QDII 估值受海外市场与汇率影响，gztime 永远滞后一个海外交易日，
        # 不能用「当天」硬卡；放宽到最近数日内仍采用，并标注估值日期。
        if not same_day and not (is_qdii and _is_recent_time(gztime)):
            return None

        calc_pct = (gsz / dwjz - 1.0) * 100.0 if gsz and dwjz and dwjz > 0 else None
        est_pct = calc_pct if calc_pct is not None else gszzl

        if est_pct is None or _is_suspicious_estimate(fund_type, est_pct): return None

        # 滞后估值（QDII）在标签里标注来源日期，例如 "QDII估值 06-06"
        message = f"QDII估值 {gztime[5:10]}" if (not same_day and len(gztime) >= 10) else None

        return EstimateResult(
            code=code, estimate_pct=est_pct, estimate_nav=gsz,
            estimate_time=gztime, source="eastmoney_gz", coverage=1.0, message=message
        )
    except Exception as exc:
        log.debug(f"[{code}] 官方估值失败: {exc}")
        return None

def _try_holding_based_estimate(code: str, fund_type: str, latest_nav: float | None,
                                holdings: list[Holding] | None = None) -> EstimateResult | None:
    if holdings is None:
        holdings = get_holdings(code)
    if not holdings: return None

    subtype = detect_fund_subtype(fund_type, holdings)
    if subtype == "etf_linked":
        etf_holdings = [h for h in holdings if h.holding_type == "etf"]
        if not etf_holdings: return None

        etf_holding = max(etf_holdings, key=lambda h: h.weight)
        quote = get_etf_realtime_quote(etf_holding.stock_code)
        if not quote: return None

        est_growth = quote.pct_change * etf_holding.weight
        message = f"ETF穿透（{etf_holding.stock_code}，覆盖 {etf_holding.weight:.0%}）" if etf_holding.weight < 0.98 else f"ETF穿透（{etf_holding.stock_code}）"

        return EstimateResult(
            code=code, estimate_pct=est_growth * 100.0,
            estimate_nav=latest_nav * (1 + est_growth) if latest_nav else None,
            estimate_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
            source="holding_calc", coverage=etf_holding.weight, message=message
        )

    if subtype in {"money", "bond"}: return None

    is_qdii = subtype == "qdii"
    equity_like = [h for h in holdings if h.holding_type in {"stock", "etf"}]
    if not equity_like: return None

    quotes = _quotes_for_holdings(equity_like)
    weighted_change, covered_weight = 0.0, 0.0

    for holding in equity_like:
        quote = quotes.get(holding.stock_code)
        if not quote: continue
        weighted_change += quote.pct_change * holding.weight
        covered_weight += holding.weight

    # QDII 前十五大持仓覆盖率天然偏低，门槛放宽到 15%
    min_coverage = 0.15 if is_qdii else 0.20
    if covered_weight < min_coverage: return None

    if is_qdii:
        # QDII 持仓集中于同质化板块（如美股科技），按覆盖率归一化外推到整只基金。
        # 实测与官方估算吻合（如 012922：覆盖52% 加权和-3.26% → 归一化-6.29%，实际约-6.26%）。
        # 覆盖率越高越准；覆盖率低时仍归一化，但 message 会标注覆盖率供判断置信度。
        est_growth = weighted_change / covered_weight
        message = f"持仓测算 覆盖{covered_weight:.0%}"
    else:
        # A股基金的持仓测算仅作东财官方估值失败时的兜底，保持非归一化（保守）
        est_growth = weighted_change
        message = f"持仓覆盖 {covered_weight:.0%}" if covered_weight < 0.80 else None

    est_pct = est_growth * 100.0
    if _is_suspicious_estimate(fund_type, est_pct): return None

    return EstimateResult(
        code=code, estimate_pct=est_pct,
        estimate_nav=latest_nav * (1 + est_growth) if latest_nav else None,
        estimate_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
        source="holding_calc", coverage=covered_weight, message=message
    )

def _fallback_latest_nav(code: str, latest_nav: float | None, daily_growth_pct: float | None) -> EstimateResult:
    return EstimateResult(
        code=code, estimate_pct=daily_growth_pct, estimate_nav=latest_nav,
        estimate_time=None, source="latest_nav", coverage=0.0, message="非实时"
    )

def get_realtime_estimate(code: str, fund_type: str = "", latest_nav: float | None = None, daily_growth_pct: float | None = None) -> EstimateResult:
    if _is_qdii_type(fund_type):
        # QDII：东财官方估值严重滞后/不可靠，持仓测算（最新海外收盘）优先，官方仅作兜底
        return (_try_holding_based_estimate(code, fund_type, latest_nav)
                or _try_eastmoney_gz_api(code, fund_type)
                or _fallback_latest_nav(code, latest_nav, daily_growth_pct))
    return (_try_eastmoney_gz_api(code, fund_type)
            or _try_holding_based_estimate(code, fund_type, latest_nav)
            or _fallback_latest_nav(code, latest_nav, daily_growth_pct))

def prefetch_estimates(codes: list[str], fund_types: dict[str, str] | None = None) -> dict[str, EstimateResult]:
    if not fund_types: fund_types = {}
    results: dict[str, EstimateResult] = {}
    qdii_set = {c for c in codes if _is_qdii_type(fund_types.get(c, ""))}
    normal_codes = [c for c in codes if c not in qdii_set]
    qdii_codes = [c for c in codes if c in qdii_set]
    gz_failed: list[str] = []

    with ThreadPoolExecutor(max_workers=min(32, len(codes) + 1)) as executor:
        # L1 东财官方估值：仅普通基金走（QDII 官方估值滞后不可靠，统一改走持仓测算）
        l1_futures = {executor.submit(_try_eastmoney_gz_api, code, fund_types.get(code, "")): code for code in normal_codes}
        for fut in as_completed(l1_futures):
            code = l1_futures[fut]
            res = fut.result()
            if res: results[code] = res
            else: gz_failed.append(code)

        # 需要持仓的：官方估值失败的普通基金 + 全部 QDII。并发实时抓取持仓（不缓存），
        # 抓到的持仓直接传给估值函数复用，避免重复请求。
        holding_codes = gz_failed + qdii_codes
        holdings_map: dict[str, list[Holding]] = {}
        if holding_codes:
            h_futures = {executor.submit(get_holdings, code): code for code in holding_codes}
            for fut in as_completed(h_futures):
                holdings_map[h_futures[fut]] = fut.result()

    # QDII：持仓测算优先 → 东财官方兜底 → 非实时
    for code in qdii_codes:
        ft = fund_types.get(code, "")
        results[code] = (_try_holding_based_estimate(code, ft, None, holdings=holdings_map.get(code))
                         or _try_eastmoney_gz_api(code, ft)
                         or _fallback_latest_nav(code, None, None))

    # 普通基金官方估值失败：持仓测算 → 非实时
    for code in gz_failed:
        ft = fund_types.get(code, "")
        results[code] = (_try_holding_based_estimate(code, ft, None, holdings=holdings_map.get(code))
                         or _fallback_latest_nav(code, None, None))

    return results


def run_selftest() -> str:
    """逐项探测各数据源连通性，返回可读报告。用于排查部署环境（哪个源不通导致覆盖率偏低）。"""
    results: list[str] = []

    def probe(name: str, fn) -> None:
        t0 = time.time()
        try:
            detail = fn()
            ok = bool(detail)
        except Exception as exc:
            ok, detail = False, str(exc)[:80]
        ms = int((time.time() - t0) * 1000)
        results.append(f"  [{'OK  ' if ok else '失败'}] {name} ({ms}ms): {detail or '无数据'}")

    def t_tencent(full_code: str) -> str:
        q = _fetch_quotes_tencent([full_code])
        rc = full_code[2:]
        return f"现价 {q[rc].current}" if rc in q else ""

    probe("腾讯·A股 (sh600519 贵州茅台)", lambda: t_tencent("sh600519"))
    probe("腾讯·美股 (usNVDA 英伟达)", lambda: t_tencent("usNVDA"))
    probe("腾讯·港股 (hk00700 腾讯控股)", lambda: t_tencent("hk00700"))
    probe("东财·官方估值 (110022)", lambda: (lambda r: f"{r.estimate_pct:+.2f}%" if r else "")(_try_eastmoney_gz_api("110022")))
    probe("东财·持仓抓取 (016665)", lambda: (lambda n: f"{n} 只持仓" if n else "")(len(_fetch_holdings_json_api("016665") or _fetch_holdings_html("016665") or [])))

    def t_yahoo() -> str:
        if _OVERSEA_DISABLED:
            return "已被环境变量 FUND_DISABLE_OVERSEA 关闭"
        sq = _yahoo_quote("2330")
        return f"台积电 {sq.pct_change:+.2%}" if sq else ""
    probe("Yahoo·台股 (2330 台积电)", t_yahoo)

    return "\n".join(["数据源自检（排查部署环境用；失败的源会导致对应市场持仓无法纳入估值）：", *results])
