#!/usr/bin/env python3
"""盤中即時行情：向證交所 MIS 查詢處置股（上市 tse、上櫃 otc）的最新價，寫入 live.json。

- 查詢對象取自 data.json（處置中、即將開始、最近 3 天內結束）。
- 只用標準函式庫。抓不到時不覆蓋舊的報價，只記錄錯誤。
- 報價沒變就不重寫檔案，避免無意義的 commit。
"""
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "data.json"
OUT = HERE / "live.json"
TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (disposition-tracker)"}
BASE = "https://mis.twse.com.tw/stock"
CHUNK = 25
RECENT_END_DAYS = 3  # 處置結束後仍查詢幾天


def make_opener():
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    op.addheaders = list(UA.items())
    try:  # 先拜訪首頁取得連線憑證（若不需要，失敗也不影響）
        op.open(f"{BASE}/index.jsp", timeout=20).read()
    except Exception:  # noqa: BLE001
        pass
    return op


def mis_get(opener, ex_ch):
    url = (f"{BASE}/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
           f"&_={int(time.time() * 1000)}")
    req = urllib.request.Request(url, headers={"Referer": f"{BASE}/fibest.jsp", **UA})
    with opener.open(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def fnum(v):
    try:
        x = float(str(v).replace(",", "").strip())
    except ValueError:
        return None
    return x if x > 0 else None


def parse_item(it):
    """MIS 一筆資料 -> (代號, 報價 dict)；沒有成交價回 None。"""
    code = str(it.get("c", "")).strip()
    z, y = fnum(it.get("z")), fnum(it.get("y"))
    d = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", str(it.get("d", "")).strip())
    if not code or z is None or not d:
        return None
    chg = round(z - y, 2) if y else None
    return code, {
        "d": "-".join(d.groups()),
        "t": str(it.get("t", "")).strip(),
        "o": fnum(it.get("o")),
        "z": z,
        "h": fnum(it.get("h")),
        "l": fnum(it.get("l")),
        "y": y,
        "chg": chg,
        "p": round(chg / y * 100, 2) if chg is not None and y else None,
    }


def targets(today):
    data = json.loads(DATA.read_text(encoding="utf-8"))
    cutoff = (today - timedelta(days=RECENT_END_DAYS)).isoformat()
    out, seen = [], set()
    for r in data.get("records", []):
        if r["end"] < cutoff:
            continue
        ex = "tse" if r["market"] == "上市" else "otc"
        key = (ex, r["code"])
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def main():
    today = datetime.now(TZ).date()
    if not DATA.exists():
        print("找不到 data.json，略過", file=sys.stderr)
        return 0
    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = {}

    quotes, error = {}, None
    try:
        tg = targets(today)
        opener = make_opener()
        for i in range(0, len(tg), CHUNK):
            part = tg[i:i + CHUNK]
            ex_ch = "|".join(f"{ex}_{code}.tw" for ex, code in part)
            payload = mis_get(opener, ex_ch)
            for it in payload.get("msgArray", []):
                q = parse_item(it)
                if q:
                    quotes[q[0]] = q[1]
            time.sleep(1)
        if tg and not quotes:
            error = "MIS 回傳沒有可解析的報價"
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    if error:
        print("即時行情失敗：", error, file=sys.stderr)
        if old.get("error") != error:  # 錯誤訊息有變才寫檔
            old["error"] = error
            OUT.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0

    if old.get("quotes") == quotes and not old.get("error"):
        print(f"報價無變動（{len(quotes)} 檔），不寫檔")
        return 0
    OUT.write_text(json.dumps({
        "updated": datetime.now(TZ).isoformat(timespec="seconds"),
        "error": None,
        "quotes": quotes,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"寫入 {len(quotes)} 檔即時報價")
    return 0


if __name__ == "__main__":
    sys.exit(main())
