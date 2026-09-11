#!/usr/bin/env python3
"""
指标警报 → Bark 推送  (GitHub Actions 版)

数据源: Nasdaq 官网 API (主) → Alpaca IEX (备)
密钥全部从环境变量读取:  BARK_KEY / ALPACA_KEY_ID / ALPACA_SECRET_KEY

用法:
  python3 price_alert.py              # 正常检查(非美股时段自动跳过)
  python3 price_alert.py --force      # 无视时段强制检查
  python3 price_alert.py --check      # 体检: 测试各数据源 + 发一条测试推送
  python3 price_alert.py --dry-run    # 不真的推送
"""
import json, os, re, sys, urllib.request, urllib.error
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

ET      = ZoneInfo("America/New_York")
HERE    = os.path.dirname(os.path.abspath(__file__))
CONFIG  = os.path.join(HERE, "config.json")
STATE   = os.path.join(HERE, "state.json")
UA      = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BARK_KEY      = os.environ.get("BARK_KEY", "").strip()
ALPACA_ID     = os.environ.get("ALPACA_KEY_ID", "").strip()
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()


def log(m):
    print(f"[{datetime.now(ET):%m-%d %H:%M:%S ET}] {m}", flush=True)


def load(p, d):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def save(p, d):
    with open(p, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")


def http(url, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return json.load(r)


def num(s):
    return float(re.sub(r"[^\d.\-]", "", str(s)))


# ============ 数据源 ============
def nasdaq_series(symbol, asset="etf"):
    """返回 (已收盘日线收盘价列表, 最新成交价, 最新成交价所属日期)"""
    today = datetime.now(ET).date()
    start = today - timedelta(days=420)
    h = http(f"https://api.nasdaq.com/api/quote/{symbol}/historical"
             f"?assetclass={asset}&fromdate={start}&todate={today}&limit=400")
    rows = h["data"]["tradesTable"]["rows"]
    hist = sorted((datetime.strptime(r["date"], "%m/%d/%Y").date(), num(r["close"])) for r in rows)

    q = http(f"https://api.nasdaq.com/api/quote/{symbol}/info?assetclass={asset}")["data"]["primaryData"]
    live = num(q["lastSalePrice"])
    ts = q.get("lastTradeTimestamp", "")
    m = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", ts)
    live_date = datetime.strptime(m.group(1), "%b %d, %Y").date() if m else today
    return hist, live, live_date


def alpaca_series(symbol, asset=None):
    if not (ALPACA_ID and ALPACA_SECRET):
        raise RuntimeError("未配置 Alpaca 密钥")
    hdr = {"APCA-API-KEY-ID": ALPACA_ID, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    today = datetime.now(ET).date()
    start = today - timedelta(days=420)
    b = http(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
             f"?timeframe=1Day&start={start}&limit=400&feed=iex&adjustment=raw", hdr)
    hist = sorted((datetime.fromisoformat(x["t"].replace("Z", "+00:00")).astimezone(ET).date(),
                   float(x["c"])) for x in b.get("bars", []))
    if not hist:
        raise RuntimeError("Alpaca 未返回任何日线")
    t = http(f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest?feed=iex", hdr)["trade"]
    live = float(t["p"])
    live_date = datetime.fromisoformat(t["t"].replace("Z", "+00:00")).astimezone(ET).date()
    return hist, live, live_date


SOURCES = [("nasdaq", nasdaq_series), ("alpaca", alpaca_series)]


def get_series(w):
    errors = []
    for name, fn in SOURCES:
        try:
            hist, live, live_date = fn(w["symbol"], *( [w["asset_class"]] if name == "nasdaq" and w.get("asset_class") else [] ))
            if len(hist) < 30:
                raise RuntimeError(f"日线数量不足({len(hist)})")
            return hist, live, live_date, name
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {e}")
    raise RuntimeError(" | ".join(errors))


# ============ 指标 ============
def rsi(closes, n):
    if len(closes) < n + 1:
        return None
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = [max(c, 0.0) for c in ch]
    l = [max(-c, 0.0) for c in ch]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(ch)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def compute(w, closes_hist, live, has_live):
    """返回 (昨收时的值, 含实时价的值, 单位名)"""
    ind = (w.get("indicator") or "price").lower()
    series_now = closes_hist + ([live] if has_live else [])
    if ind == "price":
        return (closes_hist[-1], series_now[-1], "价格")
    if ind == "rsi":
        n = int(w.get("period", 14))
        return (rsi(closes_hist, n), rsi(series_now, n), f"RSI({n})")
    raise ValueError(f"未知指标 {ind}")


# ============ 推送 ============
def bark(title, body, dry=False, level="timeSensitive"):
    if dry:
        log(f"    [dry-run] {title} | {body}")
        return True
    if not BARK_KEY:
        log("    ✗ 环境变量 BARK_KEY 未设置")
        return False
    data = json.dumps({"device_key": BARK_KEY, "title": title, "body": body,
                       "group": "行情警报", "sound": "alarm",
                       "level": level, "isArchive": 1}, ensure_ascii=False).encode()
    req = urllib.request.Request("https://api.day.app/push", data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.load(r)
        if res.get("code") == 200:
            log("    ✓ 已推送")
            return True
        log(f"    ✗ Bark 异常: {res}")
    except Exception as e:
        log(f"    ✗ 推送失败: {e}")
    return False


def market_open_now():
    now = datetime.now(ET)
    if now.weekday() > 4:
        return False
    return (now.hour, now.minute) >= (9, 30) and (now.hour, now.minute) < (16, 0)


# ============ 主流程 ============
def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    cfg = load(CONFIG, {"watches": []})
    state = load(STATE, {})
    today = datetime.now(ET).date()
    changed = False

    if "--check" in args:
        log("=== 数据源体检 ===")
        ok = []
        for name, fn in SOURCES:
            try:
                hist, live, ld, = fn("QQQ")[:3]
                log(f"  ✓ {name}: {len(hist)} 根日线, 最新 {hist[-1][0]} 收盘 {hist[-1][1]}, 实时 {live} ({ld})")
                ok.append(name)
            except Exception as e:
                log(f"  ✗ {name}: {type(e).__name__} {e}")
        bark("✅ 警报系统体检", f"可用数据源: {'、'.join(ok) or '无！'}", dry, level="active")
        return 0 if ok else 1

    if not market_open_now() and "--force" not in args:
        log("美股未开盘，跳过")
        return 0

    for w in cfg.get("watches", []):
        name = w.get("name") or w["symbol"]
        try:
            hist, live, live_date, src = get_series(w)
        except Exception as e:
            log(f"{name}: ✗ 全部数据源失败 → {e}")
            if state.get("_datafail") != str(today):
                bark("⚠️ 行情数据获取失败", f"{name}: {str(e)[:200]}", dry)
                state["_datafail"] = str(today); changed = True
            continue

        closes_hist = [c for d, c in hist if d < today]
        has_live = live_date >= today and closes_hist
        if not closes_hist:
            log(f"{name}: 历史数据异常，跳过"); continue

        prev_v, now_v, unit = compute(w, closes_hist, live, has_live)
        log(f"{name} · {unit} = {now_v:.2f}  (昨收基准 {prev_v:.2f}, 现价 {live:.2f}, 源={src}"
            + ("" if has_live else ", 无今日实时价") + ")")

        if not has_live:
            continue

        st = state.setdefault(w.get("id") or f"{w['symbol']}|{w.get('indicator','price')}{w.get('period','')}", {})
        for kind, key in (("below", "below"), ("above", "above")):
            level = w.get(key)
            if level is None:
                continue
            level = float(level)
            crossed = (prev_v >= level > now_v) if kind == "below" else (prev_v <= level < now_v)
            if not crossed:
                continue
            if st.get("last_" + kind) == str(today):
                log(f"    · 今日已就 {kind} {level} 推送过，跳过")
                continue
            arrow = "📉 跌破" if kind == "below" else "📈 突破"
            if bark(f"{name} {unit} {arrow} {level:g}",
                    f"当前 {now_v:.2f}（昨收 {prev_v:.2f}）· {w['symbol']} 现价 {live:.2f}", dry):
                st["last_" + kind] = str(today); changed = True

    if changed and not dry:
        save(STATE, state)
        log("状态已更新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
