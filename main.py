from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests, time, threading, logging, json, os
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Tehran")
except Exception:
    TZ = timezone(timedelta(hours=3, minutes=30))  # IRST fallback

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("faran")

app = FastAPI(title="Faran Trading Intelligence", version="1.2.0-signal")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

TSETMC_URL = "http://old.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "*/*",
    "Referer": "http://www.tsetmc.com/",
    "Connection": "keep-alive",
}

DEFAULT_CONFIG = {
    "bale_bot_token": "",
    "bale_chat_id": "",
    "bale_enabled": False,
    "min_score_to_notify": 45,
    "notify_signals": ["خرید قوی", "فروش قوی"],
    "poll_interval_seconds": 30,
}

CACHE = {
    "symbols": [],
    "market": {},
    "source": "TSETMC MarketWatchInit",
    "updated_at": None,
    "status": "starting",
    "error": None,
    "last_fetch_seconds": None,
    "signals_summary": {"خرید قوی": 0, "خرید": 0, "خنثی": 0, "فروش": 0, "فروش قوی": 0},
    "last_notified": {},
}
LOCK = threading.Lock()
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
CONFIG = DEFAULT_CONFIG.copy()


def load_config():
    global CONFIG
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            CONFIG = {**DEFAULT_CONFIG, **data}
            log.info("Config loaded from config.json")
        except Exception as e:
            log.warning("Failed to load config.json: %s", e)
            CONFIG = DEFAULT_CONFIG.copy()
    else:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        log.info("Created default config.json")


def num(v, default=0):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except Exception:
        return default


def parse_marketwatch(raw):
    parts = raw.strip().split("@")
    if len(parts) < 3:
        raise ValueError("پاسخ TSETMC ساختار مورد انتظار را ندارد.")
    market = {}
    try:
        state = parts[1].split(",")
        if len(state) >= 8:
            market = {
                "server_datetime": state[0],
                "tse_status": state[1],
                "index": num(state[2]),
                "index_change": num(state[3]),
                "market_value": num(state[4]),
                "trade_volume": num(state[5]),
                "trade_value": num(state[6]),
                "trade_count": num(state[7]),
            }
    except Exception:
        pass

    symbols = []
    for row in parts[2].split(";"):
        if not row.strip():
            continue
        c = row.split(",")
        if len(c) < 22:
            continue
        symbol = c[2].strip()
        if not symbol:
            continue
        last = num(c[7])
        close = num(c[6])
        yesterday = num(c[13])
        change = ((close - yesterday) / yesterday * 100) if yesterday else 0
        symbols.append({
            "ins_code": c[0],
            "isin": c[1],
            "symbol": symbol,
            "name": c[3].strip(),
            "time": c[4],
            "first": num(c[5]),
            "close": close,
            "last": last,
            "trades": num(c[8]),
            "volume": num(c[9]),
            "value": num(c[10]),
            "low": num(c[11]),
            "high": num(c[12]),
            "yesterday": yesterday,
            "eps": num(c[14]),
            "base_volume": num(c[15]),
            "flow": c[17] if len(c) > 17 else "",
            "change_pct": change,
            "best_buy": num(c[24]) if len(c) > 24 else 0,
            "status_code": c[25] if len(c) > 25 else "",
        })
    if not symbols:
        raise ValueError("داده نمادها خالی است؛ احتمال تغییر سرویس یا محدودیت دسترسی وجود دارد.")
    return market, symbols


def analyze_signal(x):
    """موتور سیگنال بر اساس snapshot لحظه‌ای. توصیه قطعی نیست."""
    score = 0
    reasons = []
    change = x["change_pct"]
    vol = x["volume"]
    base = x["base_volume"]
    value = x["value"]
    trades = x["trades"]
    last = x["last"]
    close = x["close"]
    yesterday = x["yesterday"]
    high = x["high"]
    low = x["low"]

    ratio = (vol / base) if base > 0 else 0

    # Positive
    if change > 0:
        score += 10
        reasons.append(f"تغییر مثبت ({change:.1f}٪)")
    if change >= 1.5:
        score += 8
        reasons.append("تغییر ≥ ۱.۵٪")
    if change >= 3:
        score += 7
        reasons.append("تغییر قوی ≥ ۳٪")

    if ratio >= 1:
        score += 10
        reasons.append(f"حجم ≥ حجم مبنا ({ratio:.1f}x)")
    if ratio >= 2:
        score += 12
        reasons.append(f"حجم بسیار بالا ({ratio:.1f}x)")
    if ratio >= 4:
        score += 8
        reasons.append("حجم فوق‌العاده بالا")

    if trades >= 150:
        score += 6
        reasons.append(f"معاملات فعال ({int(trades)})")
    if trades >= 400:
        score += 6
        reasons.append("معاملات پرتعداد")

    if value >= 3_000_000_000:
        score += 8
        reasons.append("ارزش معاملات مناسب")
    if value >= 10_000_000_000:
        score += 10
        reasons.append("ارزش معاملات بالا")

    if last >= close >= yesterday and yesterday > 0:
        score += 10
        reasons.append("روند صعودی قیمت")

    if high > low and last > (low + (high - low) * 0.6):
        score += 5
        reasons.append("قیمت نزدیک سقف روزانه")

    # Negative
    if change < 0:
        score -= 8
        reasons.append(f"تغییر منفی ({change:.1f}٪)")
    if change <= -1.5:
        score -= 8
        reasons.append("افت ≥ ۱.۵٪")
    if change <= -3:
        score -= 7
        reasons.append("افت شدید ≥ ۳٪")

    if last < close < yesterday and yesterday > 0:
        score -= 10
        reasons.append("روند نزولی قیمت")

    if ratio < 0.3 and base > 0:
        score -= 5
        reasons.append("حجم بسیار پایین")

    score = max(0, min(100, score))

    if score >= 70:
        signal = "خرید قوی"
    elif score >= 50:
        signal = "خرید"
    elif score >= 35:
        signal = "خنثی"
    elif score >= 20:
        signal = "فروش"
    else:
        signal = "فروش قوی"

    if value < 500_000_000 and signal in ("خرید قوی", "خرید"):
        signal = "خنثی"
        reasons.append("نقدشوندگی پایین → تضعیف سیگنال")
        score = min(score, 40)

    return {"score": score, "signal": signal, "reasons": reasons[:6]}


def send_bale(text: str) -> bool:
    token = CONFIG.get("bale_bot_token", "").strip()
    chat_id = CONFIG.get("bale_chat_id", "").strip()
    if not CONFIG.get("bale_enabled") or not token or not chat_id:
        return False
    try:
        url = f"https://tapi.bale.ai/bot{token}/sendMessage"
        r = SESSION.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if r.status_code == 200:
            return True
        log.warning("Bale error: %s %s", r.status_code, r.text[:200])
        return False
    except Exception as e:
        log.warning("Bale send failed: %s", e)
        return False


def notify_strong_signals(symbols):
    if not CONFIG.get("bale_enabled"):
        return

    allowed = set(CONFIG.get("notify_signals", ["خرید قوی", "فروش قوی"]))
    min_score = CONFIG.get("min_score_to_notify", 45)
    now = datetime.now(TZ).strftime("%H:%M")

    to_send = []
    with LOCK:
        last_notified = dict(CACHE.get("last_notified", {}))

    for x in symbols:
        sig = x.get("signal")
        if sig not in allowed:
            continue
        if x.get("score", 0) < min_score:
            continue
        key = x["ins_code"]
        if last_notified.get(key) == sig:
            continue
        to_send.append(x)
        last_notified[key] = sig

    with LOCK:
        CACHE["last_notified"] = last_notified

    if not to_send:
        return

    to_send = sorted(to_send, key=lambda z: z["score"], reverse=True)[:8]
    lines = [f"📡 <b>سیگنال‌های جدید — {now}</b>\n"]
    for x in to_send:
        emoji = "🟢" if "خرید" in x["signal"] else "🔴"
        reasons = " | ".join(x.get("reasons", [])[:3])
        lines.append(
            f"{emoji} <b>{x['symbol']}</b> — {x['signal']} (امتیاز {x['score']})\n"
            f"   تغییر: {x['change_pct']:+.1f}٪ | ارزش: {x['value']/1e9:.1f}B\n"
            f"   {reasons}"
        )
    lines.append("\n⚠️ این سیگنال‌ها بر اساس snapshot لحظه‌ای هستند و توصیه قطعی خرید/فروش نیستند.")
    send_bale("\n".join(lines))


def fetch_once():
    started = time.time()
    try:
        r = SESSION.get(TSETMC_URL, timeout=25)
        r.raise_for_status()
        raw = r.content.decode("utf-8-sig", errors="replace")
        if "General Error" in raw or "مسدود" in raw or "<html" in raw[:500].lower():
            raise RuntimeError("پاسخ سرویس TSETMC به‌جای داده بازار، صفحه خطا یا محدودیت دسترسی برگرداند.")
        market, symbols = parse_marketwatch(raw)

        summary = {"خرید قوی": 0, "خرید": 0, "خنثی": 0, "فروش": 0, "فروش قوی": 0}
        for x in symbols:
            result = analyze_signal(x)
            x["score"] = result["score"]
            x["signal"] = result["signal"]
            x["reasons"] = result["reasons"]
            x["score_label"] = "امتیاز سیگنال"
            x["currency_unit"] = "ریال"
            summary[result["signal"]] = summary.get(result["signal"], 0) + 1

        symbols.sort(key=lambda z: (z["score"], z["value"]), reverse=True)

        with LOCK:
            CACHE.update({
                "symbols": symbols,
                "market": market,
                "source": "TSETMC Old MarketWatch",
                "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
                "status": "connected",
                "error": None,
                "last_fetch_seconds": round(time.time() - started, 2),
                "signals_summary": summary,
            })
        log.info("Fetched %s instruments | signals: %s", len(symbols), summary)

        threading.Thread(target=notify_strong_signals, args=(symbols,), daemon=True).start()

    except Exception as e:
        msg = str(e)
        log.warning("TSETMC fetch failed: %s", msg)
        with LOCK:
            CACHE["status"] = "error"
            CACHE["error"] = msg
            CACHE["last_fetch_seconds"] = round(time.time() - started, 2)


def poller():
    while True:
        fetch_once()
        interval = max(15, int(CONFIG.get("poll_interval_seconds", 30)))
        time.sleep(interval)


@app.on_event("startup")
def startup():
    load_config()
    threading.Thread(target=poller, daemon=True).start()


@app.get("/")
def home():
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/api/health")
def health():
    with LOCK:
        return {
            "status": CACHE["status"],
            "source": CACHE["source"],
            "updated_at": CACHE["updated_at"],
            "error": CACHE["error"],
            "last_fetch_seconds": CACHE["last_fetch_seconds"],
            "bale_enabled": bool(CONFIG.get("bale_enabled") and CONFIG.get("bale_bot_token")),
            "signals_summary": CACHE.get("signals_summary", {}),
        }


@app.get("/api/market")
def market():
    with LOCK:
        return {
            "status": CACHE["status"],
            "source": CACHE["source"],
            "updated_at": CACHE["updated_at"],
            "error": CACHE["error"],
            "last_fetch_seconds": CACHE["last_fetch_seconds"],
            "market": CACHE["market"],
            "count": len(CACHE["symbols"]),
            "signals_summary": CACHE.get("signals_summary", {}),
            "symbols": CACHE["symbols"],
        }


@app.get("/api/signals")
def signals(signal: str = None, min_score: int = 0):
    with LOCK:
        items = list(CACHE["symbols"])
    if signal:
        items = [x for x in items if x.get("signal") == signal]
    if min_score:
        items = [x for x in items if x.get("score", 0) >= min_score]
    return {"count": len(items), "symbols": items[:200]}


@app.get("/api/symbol/{ins_code}")
def symbol(ins_code: str):
    with LOCK:
        for x in CACHE["symbols"]:
            if x["ins_code"] == ins_code:
                return x
    return {"error": "نماد در آخرین snapshot پیدا نشد."}


@app.post("/api/refresh")
def refresh():
    fetch_once()
    with LOCK:
        return {
            "status": CACHE["status"],
            "updated_at": CACHE["updated_at"],
            "error": CACHE["error"],
            "count": len(CACHE["symbols"]),
            "signals_summary": CACHE.get("signals_summary", {}),
        }


@app.get("/api/config")
def get_config():
    token = CONFIG.get("bale_bot_token", "")
    masked = (token[:6] + "..." + token[-4:]) if len(token) > 12 else ("***" if token else "")
    return {
        "bale_enabled": CONFIG.get("bale_enabled"),
        "bale_bot_token_masked": masked,
        "bale_chat_id": CONFIG.get("bale_chat_id"),
        "min_score_to_notify": CONFIG.get("min_score_to_notify"),
        "notify_signals": CONFIG.get("notify_signals"),
        "poll_interval_seconds": CONFIG.get("poll_interval_seconds"),
    }


class ConfigUpdate(BaseModel):
    bale_bot_token: str = None
    bale_chat_id: str = None
    bale_enabled: bool = None
    min_score_to_notify: int = None
    notify_signals: list = None
    poll_interval_seconds: int = None


@app.post("/api/config")
def update_config(body: ConfigUpdate):
    global CONFIG
    data = body.dict(exclude_none=True)
    CONFIG.update(data)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
        return {"ok": True, "message": "تنظیمات ذخیره شد"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/bale/test")
def test_bale():
    ok = send_bale("✅ تست ربات سیگنال Faran (بله)\nاگر این پیام را می‌بینی، اتصال بله درست است.")
    return {"ok": ok, "message": "پیام تست ارسال شد" if ok else "ارسال ناموفق — توکن و چت‌آیدی بله را بررسی کنید"}
