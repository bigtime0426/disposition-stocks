#!/usr/bin/env python3
"""抓證交所（上市）與櫃買中心（上櫃）處置股公告，輸出 data.json。

只用標準函式庫。每個市場有主要來源與備援來源；某市場全部失敗時保留舊資料，
兩個市場都失敗才以非 0 結束（讓 GitHub Actions 顯示失敗）。
"""
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).with_name("data.json")
KEEP_DAYS = 90  # 已結束的紀錄保留天數
TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (disposition-tracker)", "Accept": "application/json"}

def sources(today):
    """各市場的來源清單（全部都會抓並合併）。
    證交所頁面預設只回最近幾天公布的資料，所以另外用日期區間查最近 LOOKBACK 天，
    才不會漏掉公布較早但仍在處置中的股票。區間參數名稱未經實測，失敗時會被略過。"""
    s0 = (today - timedelta(days=LOOKBACK)).strftime("%Y%m%d")
    s1 = today.strftime("%Y%m%d")
    return {
        "上市": [
            f"https://www.twse.com.tw/rwd/zh/announcement/punish?startDate={s0}&endDate={s1}&response=json",
            "https://openapi.twse.com.tw/v1/announcement/punish",
            "https://www.twse.com.tw/rwd/zh/announcement/punish?response=json",
        ],
        "上櫃": [
            "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information",
            "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal?response=json",
        ],
    }


LOOKBACK = 45

PERIOD_RE = re.compile(
    r"(\d{2,4}[/\-.]\d{1,2}[/\-.]\d{1,2}|\d{7,8})\s*[~～〜\-–—至]\s*"
    r"(\d{2,4}[/\-.]\d{1,2}[/\-.]\d{1,2}|\d{7,8})"
)


def parse_date(s):
    """民國或西元日期字串 -> date；失敗回 None。"""
    s = str(s).strip()
    m = re.fullmatch(r"(\d{2,4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{3})(\d{2})(\d{2})|(\d{4})(\d{2})(\d{2})", s)
        if not m:
            return None
        g = [x for x in m.groups() if x]
        y, mo, d = map(int, g)
    if y < 1911:
        y += 1911
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def http_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def rows_from(payload):
    """把各種回傳格式統一成 list[dict]。"""
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and "tables" in payload[0]:
            return rows_from(payload[0]["tables"][0])
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        if "tables" in payload and payload["tables"]:
            return rows_from(payload["tables"][0])
        fields, data = payload.get("fields"), payload.get("data")
        if fields and isinstance(data, list):
            return [dict(zip(fields, r)) for r in data if isinstance(r, list)]
        for k in ("aaData", "data", "records"):
            if isinstance(payload.get(k), list):
                return rows_from(payload[k])
    return []


def pick(row, hints):
    for k, v in row.items():
        if any(h.lower() in str(k).lower() for h in hints) and v not in (None, ""):
            return str(v).strip()
    return ""


CN = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
      "八": 8, "九": 9, "十": 10}


def cn_to_int(s):
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN.get(s[1:], 0)
    if s.endswith("十"):
        return CN.get(s[0], 0) * 10
    if "十" in s:
        a, b = s.split("十", 1)
        return CN.get(a, 0) * 10 + CN.get(b, 0)
    return CN.get(s)


def match_interval(text):
    m = re.search(r"約每\s*([0-9一二兩三四五六七八九十]{1,3})\s*分鐘撮合", text or "")
    if not m:
        return ""
    n = cn_to_int(m.group(1))
    return f"{n}分鐘" if n else ""


LINK_TAIL = re.compile(r"\s*[\(（][^)）]*(?:\.html|https?:|\.\./)[^)）]*[\)）]\s*$")


def clean_name(name):
    """櫃買名稱後面會附詳情連結，例如 聯一光(../../xxx.html?code=3441)，去掉。"""
    return LINK_TAIL.sub("", str(name)).strip()


def kind_of(code):
    if (re.fullmatch(r"\d{4}[A-Z]?", code) or re.fullmatch(r"91\d{4}", code)
            or re.fullmatch(r"00\d{2,4}[A-Z]?", code)):
        return "股票/ETF"  # 含台灣存託憑證（91xxxx）
    if re.fullmatch(r"0\d{5}|[78]\d{5}", code):
        return "權證"
    if re.fullmatch(r"\d{5}", code):
        return "可轉債"
    return "其他"


def normalize(row, market):
    text_vals = [str(v) for v in row.values()]
    start = end = None
    for key_hint in ("處置起", "處置期", "Period", "起迄", "起訖"):
        v = pick(row, [key_hint])
        m = PERIOD_RE.search(v)
        if m:
            start, end = parse_date(m.group(1)), parse_date(m.group(2))
            break
    if not start:
        for v in text_vals:
            m = PERIOD_RE.search(v)
            if m:
                start, end = parse_date(m.group(1)), parse_date(m.group(2))
                break
    if not (start and end):
        return None
    code = pick(row, ["代號", "Code"])
    if not code:
        code = next((v for v in text_vals if re.fullmatch(r"\d{4,6}[A-Z]?", v.strip())), "")
    name = clean_name(pick(row, ["證券名稱", "名稱", "Name"]))
    if not code or not name:
        return None
    ann = parse_date(pick(row, ["公布日期", "Date"]) or "")
    detail = pick(row, ["處置內容", "Detail"])
    acc = pick(row, ["累計", "Accumul", "NumberOfAnnounce"])
    return {
        "market": market,
        "code": code,
        "name": name,
        "kind": kind_of(code),
        "announce": ann.isoformat() if ann else "",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": acc,
        "condition": pick(row, ["處置條件", "Reasons", "原因"])[:60],
        "measure": pick(row, ["處置措施", "Measures"])[:30],
        "interval": match_interval(detail) or match_interval(" ".join(text_vals)),
    }


def fetch_market(market, urls):
    """抓取該市場所有來源並合併；任一來源成功即視為成功。"""
    errors, used, merged = [], [], {}
    for url in urls:
        try:
            raw = rows_from(http_json(url))
            recs = [r for r in (normalize(x, market) for x in raw) if r]
            if raw and not recs:
                errors.append(f"{url}: 取得 {len(raw)} 列但無法解析")
                continue
            used.append(url)
            for r in recs:
                merged[(r["market"], r["code"], r["start"], r["end"])] = r
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {type(e).__name__}: {e}")
    if not used:
        return None, None, errors
    return list(merged.values()), used, errors


# ---------------------------------------------------------------- 每日行情
PRICE_KEEP_DAYS = 120  # 每檔股票保留的行情天數

QUOTE_SOURCES = {
    "上市": [
        "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json",
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    ],
    "上櫃": [
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
        "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?response=json&type=EW",
    ],
}


def num(v):
    """'+3.00' / '1,234.5' / '<p>+</p>2' -> float；'--'、空白、非數字 -> None。"""
    t = re.sub(r"<[^>]*>", "", str(v)).replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def payload_date(payload):
    if isinstance(payload, dict):
        if payload.get("date"):
            return parse_date(payload["date"])
        t = payload.get("tables")
        if t and isinstance(t[0], dict) and t[0].get("date"):
            return parse_date(t[0]["date"])
    return None


def normalize_quote(row, top_date):
    code = pick(row, ["證券代號", "代號", "Code"])
    o = num(pick(row, ["開盤", "Opening", "Open"]) or "")
    c = num(pick(row, ["收盤", "Closing", "Close"]) or "")
    if not code or not c or c <= 0:
        return None
    if o is not None and o <= 0:
        o = None
    chg = num(pick(row, ["漲跌價差", "漲跌", "Change"]) or "")
    d = top_date or parse_date(pick(row, ["Date", "日期"]) or "")
    if not d:
        return None
    pct = None
    if chg is not None and c - chg > 0:
        pct = round(chg / (c - chg) * 100, 2)
    return code, d.isoformat(), {"o": o, "c": c, "chg": chg, "p": pct}


def fetch_quotes(market):
    """回傳 ({code: (date, quote)}, 來源網址, 錯誤)；用第一個成功的來源。"""
    errors = []
    for url in QUOTE_SOURCES[market]:
        try:
            payload = http_json(url)
            top = payload_date(payload)
            out = {}
            for row in rows_from(payload):
                q = normalize_quote(row, top)
                if q:
                    out[q[0]] = (q[1], q[2])
            if out:
                return out, url, errors
            errors.append(f"{url}: 沒有可解析的行情")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {type(e).__name__}: {e}")
    return None, None, errors


def update_prices(prices, records, today, meta):
    """把最新行情併入 prices[code][date]；只保留處置股票/ETF 的代號。"""
    want = {r["code"]: r["market"] for r in records if r["kind"] == "股票/ETF"}
    keep_from = (today - timedelta(days=PRICE_KEEP_DAYS)).isoformat()
    prices = {c: {d: q for d, q in v.items() if d >= keep_from}
              for c, v in prices.items() if c in want}
    for market in QUOTE_SOURCES:
        quotes, used, errs = fetch_quotes(market)
        if quotes is None:
            meta[market + "行情"] = {"ok": False, "errors": errs}
            print(f"[{market}行情] 失敗：{errs}", file=sys.stderr)
            continue
        hit = 0
        for code, (d, q) in quotes.items():
            if want.get(code) == market:
                prices.setdefault(code, {})[d] = q
                hit += 1
        dates = sorted({d for d, _ in quotes.values()})
        meta[market + "行情"] = {"ok": True, "source": used, "rows": len(quotes),
                                  "matched": hit, "date": dates[-1] if dates else ""}
        print(f"[{market}行情] 全市場 {len(quotes)} 檔，處置股命中 {hit} 檔，日期 {dates[-1:]}")
    return prices


def main(argv):
    today = datetime.now(TZ).date()
    old, old_prices = [], {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
            old, old_prices = prev.get("records", []), prev.get("prices", {})
        except Exception:  # noqa: BLE001
            old, old_prices = [], {}

    merged, meta, ok_any = {}, {}, False
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for r in old:
        r["name"] = clean_name(r["name"])
        r["kind"] = kind_of(r["code"])
        if r["end"] >= cutoff:
            merged[(r["market"], r["code"], r["start"], r["end"])] = r

    for market, urls in sources(today).items():
        recs, used, errs = fetch_market(market, urls)
        if recs is None:
            meta[market] = {"ok": False, "errors": errs}
            print(f"[{market}] 失敗，保留舊資料：{errs}", file=sys.stderr)
            continue
        ok_any = True
        meta[market] = {"ok": True, "source": used, "count": len(recs), "errors": errs}
        for r in recs:
            merged[(r["market"], r["code"], r["start"], r["end"])] = r
        print(f"[{market}] {len(recs)} 筆 <- {len(used)} 個來源")

    if not ok_any:
        print("兩個市場都抓取失敗，未更新 data.json", file=sys.stderr)
        return 1

    records = sorted(merged.values(), key=lambda r: (r["start"], r["market"], r["code"]))
    prices = update_prices(old_prices, records, today, meta)
    out = {
        "updated": datetime.now(TZ).isoformat(timespec="seconds"),
        "meta": meta,
        "records": records,
        "prices": prices,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"寫入 {len(records)} 筆處置、{len(prices)} 檔行情到 {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
