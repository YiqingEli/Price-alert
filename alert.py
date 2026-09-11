#!/usr/bin/env python3
"""
行情警报 → Bark   (GitHub Actions 版)

判据(每档四项):
  波动 VIX           恐慌 CNN Fear&Greed
  强弱 QQQ日线RSI(6)  价格 纳指QQQ 或 标普SPY 任一跌幅
    一档  VIX>25  恐慌<20  RSI<30  跌>2%   → ≥2项加「加仓」
    二档  VIX>33  恐慌<10  RSI<22  跌>3%   → ≥2项加「抄底」

规则: ≥1项发警报；只发最高档；当天已推高档不再推低档；
      项数创当天新高才推；数据缺失显示⚠️且不计入分母。

密钥来自环境变量: BARK_KEY
用法: python3 alert.py [--once] [--force] [--dry-run] [--check]
"""
import json, os, re, sys, time, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET      = ZoneInfo("America/New_York")
HERE    = os.path.dirname(os.path.abspath(__file__))
CONFIG  = os.path.join(HERE, "config.json")
STATE   = os.path.join(HERE, "state.json")
UA      = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
BARK_KEY = os.environ.get("BARK_KEY", "").strip()

CNBC_URL = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
            "?symbols={}&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json")
CNN_URL  = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_HDR  = {"Accept": "application/json", "Referer": "https://edition.cnn.com/markets/fear-and-greed"}
NSDQ_HDR = {"Accept": "application/json"}

ROWS = ("波动", "恐慌", "强弱", "纳指", "标普")   # 显示顺序，全为两个汉字以保证对齐
_HIST_CACHE = {}                                  # 日线历史一个任务内只取一次


def log(m):
    print(f"[{datetime.now(ET):%m-%d %H:%M:%S ET}] {m}", flush=True)


def load(p, d):
    try:
        with open(p) as f: return json.load(f)
    except (OSError, ValueError): return d


def save(p, d):
    with open(p, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2); f.write("\n")


def http(url, headers=None, timeout=20):
    h = {"User-Agent": UA}; h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return json.load(r)


def parse_ts(s):
    if not s: return None
    s = str(s).strip().replace("Z", "+00:00")
    m = re.match(r"^(.*[+-]\d{2})(\d{2})$", s)
    if m: s = m.group(1) + ":" + m.group(2)
    try: return datetime.fromisoformat(s)
    except ValueError: return None


def num(s):
    try: return float(re.sub(r"[^\d.\-]", "", str(s)))
    except (TypeError, ValueError): return None


class Reading:
    """missing(value=None)=拿不到或是昨天的；note=附加说明"""
    def __init__(self, value=None, ts=None, source="", note=""):
        self.value, self.ts, self.source, self.note = value, ts, source, note
        self.prev = self.drop = None
    @property
    def ok(self): return self.value is not None
    def fresh_today(self, today):
        return self.ts is not None and self.ts.astimezone(ET).date() == today


# ───────────────────────── 指标 ─────────────────────────
def rsi(closes, n):
    if len(closes) < n + 1: return None
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = [max(c, 0.0) for c in ch]; l = [max(-c, 0.0) for c in ch]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n          # Wilder 平滑，同 TradingView ta.rsi
    for i in range(n, len(ch)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


# ───────────────────────── 数据源 ─────────────────────────
def fetch_cnbc():
    out, status = {}, None
    try:
        rows = http(CNBC_URL.format("QQQ|SPY|.VIX"))["FormattedQuoteResult"]["FormattedQuote"]
    except Exception as e:
        log(f"  CNBC 请求失败: {type(e).__name__} {e}"); return out, None
    for x in rows:
        status = status or x.get("curmktstatus")
        out[x.get("symbol", "")] = {"last": num(x.get("last")),
                                    "prev": num(x.get("previous_day_closing")),
                                    "chg_src": num(x.get("change_pct")),
                                    "ts": parse_ts(x.get("last_time"))}
    return out, status


def fetch_cnn():
    try:
        d = http(CNN_URL, CNN_HDR, timeout=25)
    except Exception as e:
        log(f"  CNN 请求失败: {type(e).__name__} {e}"); return Reading(), Reading()
    fg = Reading()
    try:
        f = d["fear_and_greed"]
        fg = Reading(float(f["score"]), parse_ts(f["timestamp"]), "CNN")
    except Exception: pass
    vx = Reading()
    try:
        p = d["market_volatility_vix"]["data"][-1]
        vx = Reading(float(p["y"]), datetime.fromtimestamp(p["x"] / 1000, ET), "CNN")
    except Exception: pass
    return fg, vx


def fetch_nasdaq_quote(sym):
    try:
        d = http(f"https://api.nasdaq.com/api/quote/{sym}/info?assetclass=etf",
                 NSDQ_HDR)["data"]["primaryData"]
        m = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", d.get("lastTradeTimestamp", "") or "")
        ts = datetime.strptime(m.group(1), "%b %d, %Y").replace(tzinfo=ET) if m else None
        return {"last": num(d.get("lastSalePrice")), "prev": None, "chg_src": None, "ts": ts}
    except Exception as e:
        log(f"  Nasdaq {sym} 报价失败: {type(e).__name__}"); return None


def fetch_daily_closes(sym, today):
    """已收盘的日线收盘价序列(不含今天)。一个任务内只取一次。"""
    if _HIST_CACHE.get("sym") == sym and _HIST_CACHE.get("date") == today:
        return _HIST_CACHE.get("closes")
    closes = None
    try:
        start = today - timedelta(days=400)
        rows = http(f"https://api.nasdaq.com/api/quote/{sym}/historical"
                    f"?assetclass=etf&fromdate={start}&todate={today}&limit=400",
                    NSDQ_HDR, timeout=30)["data"]["tradesTable"]["rows"]
        hist = sorted((datetime.strptime(r["date"], "%m/%d/%Y").date(), num(r["close"]))
                      for r in rows)
        closes = [c for d, c in hist if d < today and c]
        if len(closes) < 30:
            log(f"  {sym} 日线数量不足({len(closes)})"); closes = None
        else:
            log(f"  {sym} 日线历史 {len(closes)} 根，最后一根 {[d for d,_ in hist if d<today][-1]}")
    except Exception as e:
        log(f"  {sym} 日线历史失败: {type(e).__name__} {e}")
    _HIST_CACHE.update(sym=sym, date=today, closes=closes)
    return closes


# ───────────────────────── 采集 ─────────────────────────
def gather(rsi_period=6):
    today = datetime.now(ET).date()
    cnbc, mkt = fetch_cnbc()
    fg, cnn_vix = fetch_cnn()

    # 波动：CNBC 实时优先，退 CNN(延迟约18分钟)
    vix = Reading()
    c = cnbc.get(".VIX")
    if c and c["last"] is not None:
        vix = Reading(c["last"], c["ts"], "CNBC")
    if not (vix.ok and vix.fresh_today(today)):
        if cnn_vix.ok and cnn_vix.fresh_today(today):
            cnn_vix.note = "实时数据缺失"; vix = cnn_vix
        else:
            vix = Reading()

    if not (fg.ok and fg.fresh_today(today)):
        fg = Reading()

    # 价格：CNBC 优先，退 Nasdaq（Nasdaq 无昨收，借用 CNBC 的）
    prices = {}
    for label, sym in (("纳指", "QQQ"), ("标普", "SPY")):
        q = cnbc.get(sym); src = "CNBC"
        if not (q and q["last"] is not None and q["prev"]):
            q2 = fetch_nasdaq_quote(sym)
            if q2 and q2["last"] is not None and q and q["prev"]:
                q2["prev"] = q["prev"]; q = q2; src = "Nasdaq"
            else:
                prices[label] = Reading(); continue
        r = Reading(q["last"], q["ts"], src)
        if not r.fresh_today(today) or not q["prev"]:
            prices[label] = Reading(); continue
        r.prev = q["prev"]
        r.drop = (q["last"] / q["prev"] - 1) * 100
        if q.get("chg_src") is not None and abs(q["chg_src"] - r.drop) > 0.1:
            log(f"  ⚠ {label} 自算 {r.drop:+.2f}% 与源 {q['chg_src']:+.2f}% 差 "
                f"{abs(q['chg_src']-r.drop):.2f}%（可能除息）")
        prices[label] = r

    # 强弱：QQQ 日线历史 + 今日实时价 拼接算 RSI
    rs = Reading()
    qqq = prices.get("纳指", Reading())
    hist = fetch_daily_closes("QQQ", today)
    if hist and qqq.ok:
        v = rsi(hist + [qqq.value], rsi_period)
        if v is not None:
            rs = Reading(v, qqq.ts, "Nasdaq+实时")
    return {"vix": vix, "fg": fg, "rsi": rs, "prices": prices, "mkt": mkt, "today": today}


# ───────────────────────── 判定 ─────────────────────────
def judge(data, tier):
    """返回 (满足项数, 可用项数, 展示明细[(标签,读数,是否满足,要求)])"""
    v, f, r, pr = data["vix"], data["fg"], data["rsi"], data["prices"]
    items = [
        ("波动", v, (v.value > tier["vix"]) if v.ok else None, f">{tier['vix']:g}"),
        ("恐慌", f, (f.value < tier["fng"]) if f.ok else None, f"<{tier['fng']:g}"),
        ("强弱", r, (r.value < tier["rsi"]) if r.ok else None, f"<{tier['rsi']:g}"),
    ]
    hits = []
    for label in ("纳指", "标普"):
        x = pr.get(label, Reading())
        hit = (x.drop <= -tier["drop"]) if (x.ok and x.drop is not None) else None
        hits.append(hit)
        items.append((label, x, hit, f">{tier['drop']:g}%"))
    price_ok = None if all(h is None for h in hits) else any(h for h in hits if h)
    met = sum(1 for _, _, h, _ in items[:3] if h) + (1 if price_ok else 0)
    avail = sum(1 for _, _, h, _ in items[:3] if h is not None) + (0 if price_ok is None else 1)
    return met, avail, items


def mark(h): return "⚠️" if h is None else ("✅" if h else "❌")


def fmt_value(lab, r):
    if lab in ("纳指", "标普"): return f"{r.value:,.2f}  {r.drop:+.2f}%"
    if lab == "恐慌": return f"{r.value:.0f}"
    return f"{r.value:.1f}"


def build_message(data, t1, t2, tiers, active):
    m1, a1, it1 = t1
    m2, a2, it2 = t2
    lines = []
    for (lab, r, h1, _), (_, _, h2, _) in zip(it1, it2):
        if not r.ok:
            lines.append(f"⚠️⚠️ {lab} 数据缺失"); continue
        tail = f"  ({r.note})" if r.note else ""
        lines.append(f"{mark(h1)}{mark(h2)} {lab} {fmt_value(lab, r)}{tail}")
    lines += ["", "勾序＝一档｜二档，价格项取纳指/标普任一",
              f"一档 {m1}/{a1} · 二档 {m2}/{a2} · {datetime.now(ET):%m-%d %H:%M ET}"]

    met, avail, items = (t2 if active == 2 else t1)
    t = tiers[active - 1]
    if met >= 2:
        title = f"{t['label']} · {t['name']} {met}/{avail}"
    else:
        who = next((f"{lab} {fmt_value(lab, r).split('  ')[-1] if lab in ('纳指','标普') else fmt_value(lab, r)}"
                    for lab, r, h, _ in items if h), "")
        title = f"⚠️ {t['name']}触发 · {who}"
    if avail < 4:
        title += "（部分数据缺失）"
    return title, "\n".join(lines)


# ───────────────────────── 推送 ─────────────────────────
def bark(title, body, dry=False, level="timeSensitive"):
    if dry:
        log(f"  [dry-run] {title}\n" + "\n".join("      " + x for x in body.split("\n")))
        return True
    if not BARK_KEY:
        log("  ✗ 环境变量 BARK_KEY 未设置"); return False
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
            log("  ✓ 已推送"); return True
        log(f"  ✗ Bark 返回 {res}")
    except Exception as e:
        log(f"  ✗ 推送失败: {e}")
    return False


# ───────────────────────── 市场状态 ─────────────────────────
def market_open(status):
    if status: return status.upper() == "REG_MKT"
    now = datetime.now(ET)
    return now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)


def seconds_to_open():
    now = datetime.now(ET)
    return (now.replace(hour=9, minute=30, second=5, microsecond=0) - now).total_seconds()


# ───────────────────────── 主流程 ─────────────────────────
def check_once(cfg, state, dry=False):
    data = gather(cfg.get("rsi_period", 6))
    tiers = cfg["tiers"]
    t1, t2 = judge(data, tiers[0]), judge(data, tiers[1])
    p = data["prices"]
    log(f"  一档 {t1[0]}/{t1[1]} · 二档 {t2[0]}/{t2[1]}  ｜ "
        f"波动 {data['vix'].value} 恐慌 {data['fg'].value} "
        f"强弱 {data['rsi'].value and round(data['rsi'].value,1)} "
        f"纳指 {p.get('纳指',Reading()).drop and round(p['纳指'].drop,2)}% "
        f"标普 {p.get('标普',Reading()).drop and round(p['标普'].drop,2)}%")

    if t1[1] == 0 and t2[1] == 0:
        if not state.get("datafail"):
            bark("⚠️ 行情数据源全部失败", "波动/恐慌/强弱/价格四项均取不到数据，警报暂时失效。", dry)
            state["datafail"] = True
        return data["mkt"]
    state["datafail"] = False

    active = 2 if t2[0] >= 1 else (1 if t1[0] >= 1 else 0)
    if active == 0: return data["mkt"]
    if active == 1 and state.get("tier2_max", 0) > 0: return data["mkt"]
    key = f"tier{active}_max"
    met = (t2 if active == 2 else t1)[0]
    if met <= state.get(key, 0): return data["mkt"]
    title, body = build_message(data, t1, t2, tiers, active)
    if bark(title, body, dry):
        state[key] = met
    return data["mkt"]


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    cfg = load(CONFIG, None)
    if cfg is None:
        log("config.json 读取失败"); return 1
    state = load(STATE, {})
    today = str(datetime.now(ET).date())
    if state.get("date") != today:
        state = {"date": today, "tier1_max": 0, "tier2_max": 0, "datafail": False}
    before = json.dumps(state, sort_keys=True)

    if "--check" in args:
        d = gather(cfg.get("rsi_period", 6))
        log(f"市场状态={d['mkt']}")
        log(f"  波动 {d['vix'].value} ({d['vix'].source or '无数据'})")
        log(f"  恐慌 {d['fg'].value} ({d['fg'].source or '无数据'})")
        log(f"  强弱 RSI(6) {d['rsi'].value and round(d['rsi'].value,2)} ({d['rsi'].source or '无数据'})")
        for k, r in d["prices"].items():
            log(f"  {k} {r.value} 昨收{r.prev} {r.drop and round(r.drop,2)}% ({r.source or '无数据'})")
        ok = sum(1 for x in (d['vix'], d['fg'], d['rsi']) if x.ok) + sum(1 for r in d['prices'].values() if r.ok)
        bark("✅ 警报系统体检", f"市场状态 {d['mkt']}｜{ok}/5 项数据正常", dry, level="active")
        return 0

    _, mkt = fetch_cnbc()
    if not market_open(mkt) and "--force" not in args:
        gap = seconds_to_open()
        if 0 < gap <= cfg.get("wait_open_window", 8) * 60:
            log(f"距开盘 {gap/60:.1f} 分钟，等待中…")
            time.sleep(gap)
        else:
            log(f"非交易时段(status={mkt})，跳过"); return 0

    deadline = time.time() + (0 if "--once" in args else cfg.get("loop_seconds", 240))
    n = 0
    while True:
        n += 1
        log(f"第 {n} 轮")
        status = None
        try:
            status = check_once(cfg, state, dry)
        except Exception as e:
            log(f"  本轮异常: {type(e).__name__} {e}")
        if time.time() >= deadline: break
        if status and not market_open(status) and "--force" not in args:
            log("市场已收盘，结束循环"); break
        time.sleep(min(cfg.get("poll_interval", 60), max(1, deadline - time.time())))

    if json.dumps(state, sort_keys=True) != before and not dry:
        save(STATE, state); log("状态已更新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
