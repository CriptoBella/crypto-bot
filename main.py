
# file: main.py
"""
Unified 24/7 Signals Bot — Webhook (Render), CEX scanner, human-readable trade alerts

Key points:
- Webhook-only (Flask) — stable on Render
- Single background scanner thread (no alert_loop)
- Signals: EMA(12/26) + EMA200 filter (optional), RSI(14), pump filter (Δ% over window + z‑score)
- Anti-noise: min 24h quote volume, dedup window
- Risk block in message: SL/TP list, position size hint
- Platforms (no Binance): MEXC, KuCoin, Gate.io (CEX). DEX hints shown textually when privacy mode is on
- Privacy-first defaults for DE region, with toggle commands
- Config persistence to JSON file (best-effort)

ENV (Render):
  TELEGRAM_TOKEN (required)
  CHAT_ID (optional)
  WEBHOOK_BASE (https://<app>.onrender.com)
  WEBHOOK_SECRET (path segment, e.g. hook)
Optionally tune defaults with: DEFAULT_STRONG, DEFAULT_WINDOW, DEFAULT_SL, DEFAULT_TP, DEFAULT_DEBOUNCE

Start on Render:
  Build:  pip install -r requirements.txt
  Start:  python3 main.py
"""
from __future__ import annotations

import json
import os
import random
import statistics
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request
import telebot
from telebot import types

# -----------------------------
# Config / Defaults + Persistence
# -----------------------------
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("TELEGRAM_TOKEN missing in ENV")

CHAT_ID = int(os.getenv("CHAT_ID", "0") or 0)
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")  # e.g. https://crypto-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "hook")
PORT = int(os.getenv("PORT", "8080"))

CFG_FILE = os.getenv("CFG_FILE", "runtime_cfg.json")

@dataclass
class RuntimeCFG:
    # watchlists
    assets_top: List[str] = field(default_factory=lambda: ["BTC", "ETH"])  
    assets_alts: List[str] = field(default_factory=lambda: ["SOL", "TON", "XRP", "BNB", "ADA", "TRX"])  
    assets_memes: List[str] = field(default_factory=lambda: ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"])  

    # windows & thresholds
    window_sec: int = int(os.getenv("DEFAULT_WINDOW", "600").rstrip("m"))  # seconds
    strong_z: float = float(os.getenv("DEFAULT_STRONG", "2.2"))  # z-score threshold
    move_pct_top: float = 0.8
    move_pct_alt: float = 1.2
    move_pct_meme: float = 2.0

    # EMA/RSI settings
    ema_fast: int = 12
    ema_slow: int = 26
    ema_trend: int = 200
    rsi_len: int = 14
    require_trend: bool = True

    # risk & outputs
    sl_pct: float = float(os.getenv("DEFAULT_SL", "1.8"))
    tp_list: List[float] = field(default_factory=lambda: [float(x) for x in os.getenv("DEFAULT_TP", "1.5 2.5 4").split()])
    balance_usdt: float = 50.0
    risk_pct: float = 1.5  # % of balance at risk per trade

    # anti-noise
    min_quote_vol_24h: float = 5_000_000.0
    meme_quote_vol_24h: float = 10_000_000.0
    dedupe_sec: int = int(os.getenv("DEFAULT_DEBOUNCE", "900"))

    # privacy & region (defaults tuned for DE)
    privacy_mode: bool = True
    dex_only: bool = False
    show_platform: bool = False
    region: str = "DE"  # UA | DE

    # scheduler cadence (seconds)
    scan_top_sec: Tuple[int, int] = (15, 30)
    scan_alts_sec: Tuple[int, int] = (30, 60)
    scan_memes_sec: Tuple[int, int] = (60, 300)

    # feature flags
    arb_enabled: bool = False
    arb_thresh_net: float = 3.0


CFG = RuntimeCFG()
CFG_LOCK = threading.Lock()


def load_cfg():
    try:
        if os.path.exists(CFG_FILE):
            with open(CFG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            with CFG_LOCK:
                for k, v in data.items():
                    if hasattr(CFG, k):
                        setattr(CFG, k, v)
            print("[cfg] loaded")
    except Exception as e:
        print("[cfg] load error:", e)


def save_cfg():
    try:
        tmp = asdict(CFG)
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(tmp, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[cfg] save error:", e)


# -----------------------------
# Fees for CEX (rough, %)
# -----------------------------
@dataclass
class Fees:
    buy_pct: float = 0.1
    sell_pct: float = 0.1
    swap_pct: float = 0.0
    deposit_usdt: float = 0.0
    withdraw_usdt: float = 0.0


FEES: Dict[str, Fees] = {
    "MEXC": Fees(0.1, 0.1),
    "KuCoin": Fees(0.1, 0.1),
    "Gate.io": Fees(0.2, 0.2),
}

# -----------------------------
# Bot & Webhook Server
# -----------------------------
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
app = Flask(__name__)


@app.get("/health")
def _health():
    return ("OK", 200, {"Content-Type": "text/plain"})


def set_webhook_once():
    if not WEBHOOK_BASE:
        print("[webhook] WEBHOOK_BASE not set — open / to set it manually")
        return
    try:
        bot.remove_webhook()
    except Exception:
        pass
    try:
        hook_url = f"{WEBHOOK_BASE}/telegram/{WEBHOOK_SECRET}"
        ok = bot.set_webhook(url=hook_url)
        print(f"[webhook] set: {hook_url} -> {ok}")
    except Exception as e:
        print("[webhook] error:", e)


@app.get("/")
def _root():
    try:
        set_webhook_once()
        return ("Webhook attempted. Check /health and Telegram.", 200)
    except Exception as e:
        return (f"Webhook error: {e}", 500)


@app.post(f"/telegram/<secret>")
def _telegram(secret: str):
    if secret != WEBHOOK_SECRET:
        return ("forbidden", 403)
    try:
        update = telebot.types.Update.de_json(request.data.decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        print("[webhook] process error:", e)
    return ("!", 200)


# -----------------------------
# Price Providers (CEX)
# -----------------------------

def _http_get(url: str, timeout: int = 8) -> Optional[dict | list]:
    for _ in range(2):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "SignalsBot/1.0"})
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(0.4)
    return None


# MEXC

def mexc_book(symbol: str) -> Optional[Tuple[float, float]]:
    d = _http_get(f"https://api.mexc.com/api/v3/ticker/bookTicker?symbol={symbol}USDT")
    if isinstance(d, dict):
        try:
            bid = float(d.get("bidPrice", 0) or 0)
            ask = float(d.get("askPrice", 0) or 0)
            return (bid, ask) if bid > 0 and ask > 0 else None
        except Exception:
            return None
    return None


def mexc_24h(symbol: str) -> Optional[float]:
    d = _http_get(f"https://api.mexc.com/api/v3/ticker/24hr?symbol={symbol}USDT")
    if isinstance(d, dict):
        try:
            qv = float(d.get("quoteVolume", 0) or 0)
            return qv
        except Exception:
            return None
    return None


# KuCoin

def kucoin_book(symbol: str) -> Optional[Tuple[float, float]]:
    d = _http_get(f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}-USDT")
    if isinstance(d, dict) and d.get("code") == "200000":
        data = d.get("data", {})
        try:
            bid = float(data.get("bestBidPrice", data.get("bestBid", 0)) or 0)
            ask = float(data.get("bestAskPrice", data.get("bestAsk", 0)) or 0)
            return (bid, ask) if bid > 0 and ask > 0 else None
        except Exception:
            return None
    return None


def kucoin_24h(symbol: str) -> Optional[float]:
    d = _http_get(f"https://api.kucoin.com/api/v1/market/stats?symbol={symbol}-USDT")
    if isinstance(d, dict) and d.get("code") == "200000":
        data = d.get("data", {})
        try:
            qv = float(data.get("volValue", 0) or 0)
            return qv
        except Exception:
            return None
    return None


# Gate.io

def gate_book(symbol: str) -> Optional[Tuple[float, float]]:
    d = _http_get(f"https://api.gateio.ws/api/v4/spot/order_book?currency_pair={symbol}_USDT&limit=1")
    if isinstance(d, dict):
        try:
            bids = d.get("bids", [])
            asks = d.get("asks", [])
            bid = float(bids[0][0]) if bids else 0.0
            ask = float(asks[0][0]) if asks else 0.0
            return (bid, ask) if bid > 0 and ask > 0 else None
        except Exception:
            return None
    return None


def gate_24h(symbol: str) -> Optional[float]:
    d = _http_get(f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={symbol}_USDT")
    if isinstance(d, list) and d:
        try:
            qv = float(d[0].get("quote_volume", 0) or 0)
            return qv
        except Exception:
            return None
    return None


CEX_FUN = {
    "MEXC": (mexc_book, mexc_24h),
    "KuCoin": (kucoin_book, kucoin_24h),
    "Gate.io": (gate_book, gate_24h),
}

# -----------------------------
# Series, EMA/RSI, pump/accel
# -----------------------------
@dataclass
class Series:
    prices: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=6000))  # (ts, price)
    ema_f: Optional[float] = None
    ema_s: Optional[float] = None
    ema_t: Optional[float] = None
    rsi_avg_gain: Optional[float] = None
    rsi_avg_loss: Optional[float] = None
    last_price: Optional[float] = None

    def add(self, price: float):
        ts = time.time()
        self.prices.append((ts, price))
        kf = 2 / (CFG.ema_fast + 1)
        ks = 2 / (CFG.ema_slow + 1)
        kt = 2 / (CFG.ema_trend + 1)
        self.ema_f = price if self.ema_f is None else (price - self.ema_f) * kf + self.ema_f
        self.ema_s = price if self.ema_s is None else (price - self.ema_s) * ks + self.ema_s
        self.ema_t = price if self.ema_t is None else (price - self.ema_t) * kt + self.ema_t
        if self.last_price is not None:
            change = price - self.last_price
            gain = max(0.0, change)
            loss = max(0.0, -change)
            if self.rsi_avg_gain is None:
                self.rsi_avg_gain = gain
                self.rsi_avg_loss = loss
            else:
                n = CFG.rsi_len
                self.rsi_avg_gain = (self.rsi_avg_gain * (n - 1) + gain) / n
                self.rsi_avg_loss = (self.rsi_avg_loss * (n - 1) + loss) / n
        self.last_price = price

    def rsi(self) -> Optional[float]:
        if self.rsi_avg_gain is None or self.rsi_avg_loss is None:
            return None
        if self.rsi_avg_loss == 0:
            return 100.0
        rs = self.rsi_avg_gain / self.rsi_avg_loss
        return 100 - (100 / (1 + rs))

    def pct_move(self, seconds: int) -> Optional[float]:
        if not self.prices:
            return None
        cutoff = time.time() - seconds
        base = None
        for ts, p in self.prices:
            if ts >= cutoff:
                base = p
                break
        if base is None:
            base = self.prices[0][1]
        last = self.prices[-1][1]
        return ((last - base) / base * 100.0) if base > 0 else None

    def zscore(self, window: int = 30) -> Optional[float]:
        if len(self.prices) < window + 1:
            return None
        returns = []
        last = None
        for _, p in list(self.prices)[-window - 1:]:
            if last is not None and last > 0:
                returns.append((p - last) / last)
            last = p
        if len(returns) < 3:
            return None
        m = statistics.mean(returns)
        s = statistics.pstdev(returns) or 1e-9
        cur = returns[-1]
        return (cur - m) / s


SERIES: Dict[str, Series] = defaultdict(Series)
LAST_SENT: Dict[str, float] = {}


# -----------------------------
# Helpers: platforms & messages
# -----------------------------
DEX_BY_ASSET = {
    "BTC": ["PancakeSwap v3 (BSC)"],
    "ETH": ["Uniswap v3 (Arbitrum)", "Uniswap v3 (Polygon)"],
    "SOL": ["Jupiter (Solana)", "Raydium (Solana)"],
    "TRX": ["SunSwap (TRON)"],
    "USDT": ["Jupiter (Solana)", "PancakeSwap v3 (BSC)"],
    "WIF": ["Jupiter (Solana)"],
    "BONK": ["Jupiter (Solana)", "Raydium (Solana)"],
    "PEPE": ["Uniswap v3 (Arbitrum)"],
    "FLOKI": ["PancakeSwap v3 (BSC)"],
}

CEX_BY_REGION = {
    "UA": ["MEXC", "Gate.io", "KuCoin", "Bybit"],
    "DE": ["Kraken", "Bitstamp", "Gate.io", "MEXC"],  # advisory-only list
}

TRADE_URLS = {
    "MEXC": "https://www.mexc.com/exchange/{asset}_USDT",
    "KuCoin": "https://www.kucoin.com/trade/{asset}-USDT",
    "Gate.io": "https://www.gate.io/trade/{asset}_USDT",
}


def suggest_platform(symbol: str) -> Tuple[str, str]:
    s = symbol.upper()
    if CFG.privacy_mode or CFG.dex_only:
        lst = DEX_BY_ASSET.get(s, [])
        if lst:
            return ("DEX", lst[0])
    lst = CEX_BY_REGION.get(CFG.region, [])
    if lst:
        return ("CEX", lst[0])
    return ("CEX", "Gate.io")


def position_size(entry: float, sl_price: float) -> float:
    # WHY: keep sizing conservative and deterministic for small balances
    risk_usd = CFG.balance_usdt * (CFG.risk_pct / 100.0)
    per_unit = max(1e-8, abs(entry - sl_price))
    qty = risk_usd / per_unit
    notional = qty * entry
    return round(min(notional, CFG.balance_usdt), 2)


def build_trade_message(
    symbol: str,
    direction: str,  # LONG/SHORT
    price: float,
    sl_price: float,
    tp_prices: List[float],
    reason: str,
    move_pct: Optional[float] = None,
    rsi: Optional[float] = None,
    vol_hint: Optional[str] = None,
) -> Tuple[str, Optional[types.InlineKeyboardMarkup]]:
    kind, platform = suggest_platform(symbol)
    side = "BUY" if direction == "LONG" else "SELL"
    move_txt = f" | Δ {move_pct:+.2f}%" if move_pct is not None else ""
    rsi_txt = f"RSI {rsi:.0f}" if rsi is not None else "RSI n/a"
    platform_line = (
        f"Платформа: <b>{platform}</b>" if (CFG.show_platform and platform) else "Платформа: <i>скрыто</i>"
    )
    tp_part = ", ".join([f"{p:,.2f}$" for p in tp_prices])
    sl_pct = (price - sl_price) / price * 100.0 if direction == "LONG" else (sl_price - price) / price * 100.0
    size_usd = position_size(price, sl_price)

    text = (
        f"⚡ <b>{symbol}/USDT</b> | {direction}{move_txt}\n"
        f"Цена: <b>{price:,.2f}$</b>\n"
        f"РЕКОМЕНДАЦИЯ: <b>{side}</b>  ▶ {platform_line}\n"
        f"SL: <b>{sl_price:,.2f}$</b> ({sl_pct:.2f}%) | TP: {tp_part}\n"
        f"Размер: ~{size_usd:,.2f} USDT\n"
        f"Причина: {reason} | {rsi_txt}{(' | ' + vol_hint) if vol_hint else ''}"
    )

    kb = None
    if kind == "CEX" and CFG.show_platform and platform in TRADE_URLS:
        url = TRADE_URLS[platform].format(asset=symbol)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(text=f"Открыть в {platform}", url=url))
    elif kind == "DEX" and CFG.show_platform:
        text += "\nСети с низкими комиссиями: Solana/BSC/Arbitrum/Polygon"

    return text, kb


def safe_send(text: str, kb: Optional[types.InlineKeyboardMarkup] = None):
    global CHAT_ID
    if CHAT_ID == 0:
        print("[send skipped] CHAT_ID not set yet")
        return
    try:
        bot.send_message(CHAT_ID, text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        print("[telegram] send_message error:", e)


# -----------------------------
# Signals Engine
# -----------------------------

def mid_price(symbol: str) -> Optional[float]:
    vals = []
    for _ex, (book_fn, _vol_fn) in CEX_FUN.items():
        q = book_fn(symbol)
        if q:
            bid, ask = q
            vals.append((bid + ask) / 2)
        time.sleep(0.05)
    if not vals:
        return None
    return sum(vals) / len(vals)


def vol24_quote(symbol: str) -> float:
    total = 0.0
    for _ex, (_book, vol_fn) in CEX_FUN.items():
        v = vol_fn(symbol)
        if v:
            total += v
        time.sleep(0.02)
    return total


def should_signal(symbol: str, ser: Series) -> Tuple[Optional[str], Optional[str]]:
    if ser.ema_f is None or ser.ema_s is None or ser.last_price is None:
        return (None, None)
    direction = "LONG" if ser.ema_f >= ser.ema_s else "SHORT"
    cross = "EMA BULL cross" if direction == "LONG" else "EMA BEAR cross"

    if CFG.require_trend and ser.ema_t is not None:
        if direction == "LONG" and ser.last_price < ser.ema_t:
            return (None, None)
        if direction == "SHORT" and ser.last_price > ser.ema_t:
            return (None, None)

    move = ser.pct_move(CFG.window_sec) or 0.0
    z = ser.zscore() or 0.0

    s = symbol.upper()
    base = CFG.move_pct_top if s in CFG.assets_top else (CFG.move_pct_alt if s in CFG.assets_alts else CFG.move_pct_meme)

    if abs(move) < base:
        return (None, None)
    if abs(z) < CFG.strong_z:
        return (None, None)
    if direction == "LONG" and z < 0:
        return (None, None)
    if direction == "SHORT" and z > 0:
        return (None, None)

    reason = (f"{cross}, над EMA200" if (CFG.require_trend and direction == "LONG") else cross)
    reason += f", z={z:.1f}, Δ{move:+.2f}%/{int(CFG.window_sec/60)}m"
    return (direction, reason)


def dedupe_key(symbol: str, direction: str) -> str:
    return f"{symbol}:{direction}:{int(time.time() / CFG.dedupe_sec)}"


# -----------------------------
# Arbitrage (optional, CEX↔CEX)
# -----------------------------

def apply_fees(px: float, f: Fees, side: str) -> float:
    if side == "buy":
        return px * (1 + f.buy_pct / 100 + f.swap_pct / 100) + f.deposit_usdt
    else:
        return px * (1 - f.sell_pct / 100 - f.swap_pct / 100) - f.withdraw_usdt


def scan_arbitrage(symbols: Iterable[str]) -> List[Tuple[str, str, str, float]]:
    quotes: Dict[str, Dict[str, Tuple[float, float]]] = {ex: {} for ex in CEX_FUN}
    for ex, (book_fn, _vol_fn) in CEX_FUN.items():
        for s in symbols:
            q = book_fn(s)
            if q:
                quotes[ex][s] = q
            time.sleep(0.03)
    out = []
    for s in symbols:
        for buy_ex, qs in quotes.items():
            if s not in qs:
                continue
            bid_buy, ask_buy = qs[s]
            buy_net = apply_fees(ask_buy, FEES.get(buy_ex, Fees()), "buy")
            for sell_ex, qs2 in quotes.items():
                if sell_ex == buy_ex or s not in qs2:
                    continue
                bid_sell, _ask_sell = qs2[s]
                sell_net = apply_fees(bid_sell, FEES.get(sell_ex, Fees()), "sell")
                if buy_net <= 0:
                    continue
                net = (sell_net - buy_net) / buy_net * 100
                if net >= CFG.arb_thresh_net:
                    out.append((s, buy_ex, sell_ex, net))
    out.sort(key=lambda x: x[3], reverse=True)
    return out[:3]


# -----------------------------
# Scheduler loop (single, resilient)
# -----------------------------
STOP_EVENT = threading.Event()


def _scan_cycle(symbols: List[str]):
    for s in symbols:
        try:
            px = mid_price(s)
            if px is None:
                continue
            ser = SERIES[s]
            ser.add(px)

            qv = vol24_quote(s)
            min_qv = CFG.meme_quote_vol_24h if s in CFG.assets_memes else CFG.min_quote_vol_24h
            if qv < min_qv:
                continue

            direction, reason = should_signal(s, ser)
            if not direction:
                continue

            key = dedupe_key(s, direction)
            if time.time() - LAST_SENT.get(key, 0) < CFG.dedupe_sec:
                continue
            LAST_SENT[key] = time.time()

            entry = ser.last_price or px
            if direction == "LONG":
                sl = entry * (1 - CFG.sl_pct / 100)
                tp_prices = [entry * (1 + x / 100) for x in CFG.tp_list]
            else:
                sl = entry * (1 + CFG.sl_pct / 100)
                tp_prices = [entry * (1 - x / 100) for x in CFG.tp_list]

            rsi_val = ser.rsi()
            vol_hint = f"24h vol ≈ {qv/1_000_000:.1f}M"
            text, kb = build_trade_message(
                symbol=s,
                direction=direction,
                price=entry,
                sl_price=round(sl, 6),
                tp_prices=[round(x, 6) for x in tp_prices],
                reason=reason,
                move_pct=ser.pct_move(CFG.window_sec),
                rsi=rsi_val,
                vol_hint=vol_hint,
            )
            safe_send(text, kb)
        except Exception as e:
            print("[scan] error", s, e)


def _runner():
    print(">>> [scanner] starting...")
    while not STOP_EVENT.is_set():
        try:
            t0 = time.time()
            _scan_cycle(CFG.assets_top)
            time.sleep(random.uniform(*CFG.scan_top_sec))
            _scan_cycle(CFG.assets_alts)
            time.sleep(random.uniform(*CFG.scan_alts_sec))
            _scan_cycle(CFG.assets_memes)
            time.sleep(random.uniform(*CFG.scan_memes_sec))

            if CFG.arb_enabled:
                try:
                    opps = scan_arbitrage(CFG.assets_top + CFG.assets_alts)
                    for (asset, buy_ex, sell_ex, net) in opps:
                        txt = (
                            "🔥 Связка найдена\n"
                            f"Buy: {buy_ex} — 1 {asset} по рынку\n"
                            f"Sell: {sell_ex} — 1 {asset} по рынку\n"
                            f"Net-профит: +{net:.2f}% (после комиссий)\n"
                            f"Рекомм. объём: ~{min(40.0, CFG.balance_usdt):.2f} USDT"
                        )
                        safe_send(txt)
                except Exception as e:
                    print("[arb] error:", e)

            dt = time.time() - t0
            if dt < 1:
                time.sleep(1)
        except Exception as e:
            print("[scanner] loop error:", e)
            time.sleep(2)


# Launch background scanner thread at import-time so it runs under Flask
threading.Thread(target=_runner, daemon=True).start()

# -----------------------------
# Commands
# -----------------------------
SIGNALS_ON: bool = True  # reserved (signals engine always on in this build)


def _persist_after(fn):
    def _wrap(m):
        try:
            fn(m)
        finally:
            save_cfg()
    return _wrap


@bot.message_handler(commands=["start"])
def _cmd_start(m):
    global CHAT_ID
    if CHAT_ID == 0:
        CHAT_ID = m.chat.id
    bot.reply_to(
        m,
        (
            "Привет! Я 24/7 сканирую рынок и присылаю понятные сигналы.\n"
            "/help — команды\n"
            "/status — текущие параметры\n"
            "По умолчанию включена приватность (DE): платформы скрыты."
        ),
    )


@bot.message_handler(commands=["help"])
def _cmd_help(m):
    bot.reply_to(
        m,
        (
            "Команды:\n"
            "/status\n"
            "/mode aggressive|standard|safe — быстрые профили\n"
            "/set_assets <список,через,запятую> — обновить листы\n"
            "/set_window <сек> или <Xm> — окно Δ%/z\n"
            "/set_strong <σ> — порог z-score\n"
            "/set_sl <pct> | /set_tp <p1 p2 p3>\n"
            "/ema200 on|off | /set_rsi <n> | /debounce <sec>\n"
            "/arb_on|/arb_off | /arb_thresh <pct>\n"
            "/privacy_on|off | /dex_only_on|off | /show_platform_on|off | /set_region UA|DE\n"
            "/privacy_profile_de — быстрые настройки приватности (DE)\n"
            "/note <текст> | /notes — заметки/цели\n"
            "/p2p_tips | /privacy_tips — справка по P2P и приватности"
        ),
    )


@bot.message_handler(commands=["status"])
def _cmd_status(m):
    with CFG_LOCK:
        lines = []
        lines.append(f"Assets TOP: {', '.join(CFG.assets_top)}")
        lines.append(f"Assets ALTS: {', '.join(CFG.assets_alts)}")
        lines.append(f"Assets MEMES: {', '.join(CFG.assets_memes)}")
        lines.append(f"Window: {CFG.window_sec}s | strong z≥{CFG.strong_z}")
        lines.append(
            f"EMA: {CFG.ema_fast}/{CFG.ema_slow}, trend200={'ON' if CFG.require_trend else 'OFF'} | RSI={CFG.rsi_len}"
        )
        lines.append(
            f"Risk: SL {CFG.sl_pct}% | TP {', '.join([str(x) for x in CFG.tp_list])}% | balance {CFG.balance_usdt}$ | risk {CFG.risk_pct}%"
        )
        lines.append(
            f"Anti-noise: min24hVol {CFG.min_quote_vol_24h/1_000_000:.1f}M, memes {CFG.meme_quote_vol_24h/1_000_000:.1f}M | debounce {CFG.dedupe_sec}s"
        )
        lines.append(
            f"Arb: {'ON' if CFG.arb_enabled else 'OFF'} (≥{CFG.arb_thresh_net}%) | Privacy: {'ON' if CFG.privacy_mode else 'OFF'}, DEX-only: {'ON' if CFG.dex_only else 'OFF'}, Show platform: {'ON' if CFG.show_platform else 'OFF'}, Region: {CFG.region}"
        )
        bot.reply_to(m, "\n".join(lines))


@bot.message_handler(commands=["set_assets"])
@_persist_after
def _cmd_assets(m):
    raw = m.text.split(" ", 1)
    if len(raw) < 2:
        bot.reply_to(m, "Использование: /set_assets BTC,ETH,SOL,… (все классы)")
        return
    parts = [x.strip().upper() for x in raw[1].replace(";", ",").split(",") if x.strip()]
    if not parts:
        bot.reply_to(m, "Пустой список")
        return
    top = [x for x in parts if x in ("BTC", "ETH")]
    memes = [x for x in parts if x in CFG.assets_memes]
    alts = [x for x in parts if x not in top and x not in memes]
    with CFG_LOCK:
        CFG.assets_top = top or CFG.assets_top
        CFG.assets_alts = alts or CFG.assets_alts
        CFG.assets_memes = memes or CFG.assets_memes
    bot.reply_to(m, f"OK. Обновлено. TOP={CFG.assets_top}, ALTS={CFG.assets_alts}, MEMES={CFG.assets_memes}")


@bot.message_handler(commands=["set_window"])
@_persist_after
def _cmd_window(m):
    parts = m.text.strip().split()
    if len(parts) != 2:
        bot.reply_to(m, "Использование: /set_window 600 или 10m")
        return
    v = parts[1].lower()
    try:
        if v.endswith("m"):
            secs = int(float(v[:-1]) * 60)
        else:
            secs = int(v)
        with CFG_LOCK:
            CFG.window_sec = max(30, secs)
        bot.reply_to(m, f"OK. Window={CFG.window_sec}s")
    except Exception:
        bot.reply_to(m, "Формат: 600 или 10m")


@bot.message_handler(commands=["set_strong"])
@_persist_after
def _cmd_strong(m):
    parts = m.text.strip().split()
    try:
        with CFG_LOCK:
            CFG.strong_z = float(parts[1])
        bot.reply_to(m, f"OK. strong z≥{CFG.strong_z}")
    except Exception:
        bot.reply_to(m, "Использование: /set_strong 2.2")


@bot.message_handler(commands=["set_sl"])
@_persist_after
def _cmd_sl(m):
    parts = m.text.strip().split()
    try:
        with CFG_LOCK:
            CFG.sl_pct = float(parts[1])
        bot.reply_to(m, f"OK. SL={CFG.sl_pct}%")
    except Exception:
        bot.reply_to(m, "Использование: /set_sl 1.8")


@bot.message_handler(commands=["set_tp"])
@_persist_after
def _cmd_tp(m):
    raw = m.text.split(" ", 1)
    if len(raw) < 2:
        bot.reply_to(m, "Использование: /set_tp 1.5 2.5 4")
        return
    try:
        parts = [float(x) for x in raw[1].split() if x.strip()]
        if not parts:
            raise ValueError
        with CFG_LOCK:
            CFG.tp_list = parts
        bot.reply_to(m, f"OK. TP set: {CFG.tp_list}")
    except Exception:
        bot.reply_to(m, "Формат: числа через пробел")


@bot.message_handler(commands=["ema200"])
@_persist_after
def _cmd_ema200(m):
    parts = m.text.strip().split()
    if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
        bot.reply_to(m, "Использование: /ema200 on|off")
        return
    with CFG_LOCK:
        CFG.require_trend = parts[1].lower() == "on"
    bot.reply_to(m, f"EMA200 filter: {'ON' if CFG.require_trend else 'OFF'}")


@bot.message_handler(commands=["set_rsi"])
@_persist_after
def _cmd_rsi(m):
    parts = m.text.strip().split()
    try:
        with CFG_LOCK:
            CFG.rsi_len = max(2, int(parts[1]))
        bot.reply_to(m, f"OK. RSI len={CFG.rsi_len}")
    except Exception:
        bot.reply_to(m, "Использование: /set_rsi 14")


@bot.message_handler(commands=["debounce"])
@_persist_after
def _cmd_debounce(m):
    parts = m.text.strip().split()
    try:
        with CFG_LOCK:
            CFG.dedupe_sec = max(60, int(parts[1]))
        bot.reply_to(m, f"OK. debounce={CFG.dedupe_sec}s")
    except Exception:
        bot.reply_to(m, "Использование: /debounce 1200")


@bot.message_handler(commands=["arb_on"])
@_persist_after
def _cmd_arb_on(m):
    with CFG_LOCK:
        CFG.arb_enabled = True
    bot.reply_to(m, "Арбитраж: ON")


@bot.message_handler(commands=["arb_off"])
@_persist_after
def _cmd_arb_off(m):
    with CFG_LOCK:
        CFG.arb_enabled = False
    bot.reply_to(m, "Арбитраж: OFF")


@bot.message_handler(commands=["arb_thresh"])
@_persist_after
def _cmd_arb_thr(m):
    parts = m.text.strip().split()
    try:
        with CFG_LOCK:
            CFG.arb_thresh_net = float(parts[1])
        bot.reply_to(m, f"OK. arb threshold net={CFG.arb_thresh_net}%")
    except Exception:
        bot.reply_to(m, "Использование: /arb_thresh 3.0")


@bot.message_handler(commands=["privacy_on"])
@_persist_after
def _p_on(m):
    with CFG_LOCK:
        CFG.privacy_mode = True
    bot.reply_to(m, "Privacy mode: ON (DEX в приоритете, можно скрыть платформы)")


@bot.message_handler(commands=["privacy_off"])
@_persist_after
def _p_off(m):
    with CFG_LOCK:
        CFG.privacy_mode = False
    bot.reply_to(m, "Privacy mode: OFF")


@bot.message_handler(commands=["dex_only_on"])
@_persist_after
def _d_on(m):
    with CFG_LOCK:
        CFG.dex_only = True
    bot.reply_to(m, "DEX-only: ON")


@bot.message_handler(commands=["dex_only_off"])
@_persist_after
def _d_off(m):
    with CFG_LOCK:
        CFG.dex_only = False
    bot.reply_to(m, "DEX-only: OFF")


@bot.message_handler(commands=["show_platform_on"])
@_persist_after
def _sp_on(m):
    with CFG_LOCK:
        CFG.show_platform = True
    bot.reply_to(m, "Показывать платформу: ON")


@bot.message_handler(commands=["show_platform_off"])
@_persist_after
def _sp_off(m):
    with CFG_LOCK:
        CFG.show_platform = False
    bot.reply_to(m, "Показывать платформу: OFF")


@bot.message_handler(commands=["set_region"])
@_persist_after
def _set_region(m):
    parts = m.text.strip().split()
    if len(parts) == 2 and parts[1].upper() in ("UA", "DE"):
        with CFG_LOCK:
            CFG.region = parts[1].upper()
        bot.reply_to(m, f"Регион: {CFG.region}")
    else:
        bot.reply_to(m, "Использование: /set_region UA|DE")


# -------- Fast Modes (mapped to real fields) --------
@bot.message_handler(commands=["mode"])
def _mode_root(m):
    parts = m.text.strip().split()
    if len(parts) == 1:
        bot.reply_to(
            m,
            (
                "Использование: /mode aggressive | standard | safe\n"
                "aggressive — больше сигналов (вкл. мемы), короткие SL/TP\n"
                "standard — баланс сигналов/качества\n"
                "safe — строгие фильтры, меньше сигналов"
            ),
        )
        return
    arg = parts[1].lower()
    if arg.startswith("aggr"):
        _mode_aggressive(m)
    elif arg.startswith("stan") or arg.startswith("std"):
        _mode_standard(m)
    elif arg.startswith("safe"):
        _mode_safe(m)
    else:
        bot.reply_to(m, "Неизвестный режим. Используй: aggressive | standard | safe")


@_persist_after
def _mode_aggressive(m):
    with CFG_LOCK:
        CFG.assets_top = ["BTC", "ETH"]
        CFG.assets_alts = ["SOL", "XRP", "BNB"]
        CFG.assets_memes = ["DOGE", "PEPE", "FLOKI", "WIF", "BONK"]
        CFG.window_sec = 180
        CFG.strong_z = 1.6
        CFG.sl_pct = 0.3
        CFG.tp_list = [0.6, 1.2, 2.0]
        CFG.require_trend = False
        CFG.rsi_len = 9
        CFG.dedupe_sec = 300
    bot.reply_to(
        m,
        (
            "⚡ Режим: AGGRESSIVE\n"
            "Активы: BTC, ETH; альты: SOL, XRP, BNB; мемы: DOGE, PEPE, FLOKI, WIF, BONK\n"
            "Окно 3m | z≥1.6 | SL 0.3% | TP 0.6/1.2/2.0% | EMA200 OFF | RSI 9 | Debounce 300s"
        ),
    )


@_persist_after
def _mode_standard(m):
    with CFG_LOCK:
        CFG.assets_top = ["BTC", "ETH"]
        CFG.assets_alts = ["SOL", "TON", "XRP", "BNB", "ADA"]
        CFG.assets_memes = ["DOGE", "PEPE"]
        CFG.window_sec = 300
        CFG.strong_z = 2.0
        CFG.sl_pct = 0.4
        CFG.tp_list = [0.8, 1.6, 3.0]
        CFG.require_trend = True
        CFG.rsi_len = 14
        CFG.dedupe_sec = 600
    bot.reply_to(
        m,
        (
            "✅ Режим: STANDARD\n"
            "Активы: BTC, ETH; альты: SOL, TON, XRP, BNB, ADA; мемы: DOGE, PEPE\n"
            "Окно 5m | z≥2.0 | SL 0.4% | TP 0.8/1.6/3.0% | EMA200 ON | RSI 14 | Debounce 600s"
        ),
    )


@_persist_after
def _mode_safe(m):
    with CFG_LOCK:
        CFG.assets_top = ["BTC", "ETH"]
        CFG.assets_alts = ["SOL", "TON", "ADA"]
        CFG.assets_memes = []
        CFG.window_sec = 600
        CFG.strong_z = 2.6
        CFG.sl_pct = 0.6
        CFG.tp_list = [1.5, 3.0, 5.0]
        CFG.require_trend = True
        CFG.rsi_len = 14
        CFG.dedupe_sec = 1200
    bot.reply_to(
        m,
        (
            "🛡 Режим: SAFE\n"
            "Активы: BTC, ETH; альты: SOL, TON, ADA\n"
            "Окно 10m | z≥2.6 | SL 0.6% | TP 1.5/3.0/5.0% | EMA200 ON | RSI 14 | Debounce 1200s"
        ),
    )


# -------- Notes & Tips (goals, privacy) --------
NOTES_FILE = os.getenv("NOTES_FILE", "notes.json")


def _load_notes() -> List[str]:
    try:
        if os.path.exists(NOTES_FILE):
            with open(NOTES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_notes(items: List[str]):
    try:
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@bot.message_handler(commands=["note"])
def _cmd_note(m):
    raw = m.text.split(" ", 1)
    if len(raw) < 2 or not raw[1].strip():
        bot.reply_to(m, "Формат: /note цель/заметка")
        return
    items = _load_notes()
    items.append(raw[1].strip())
    _save_notes(items)
    bot.reply_to(m, "OK. Добавлено в заметки.")


@bot.message_handler(commands=["notes"])
def _cmd_notes(m):
    items = _load_notes()
    if not items:
        bot.reply_to(m, "Пока пусто. Добавь через /note <текст>.")
    else:
        bot.reply_to(m, "Ваши заметки/цели:\n- " + "\n- ".join(items[-20:]))


@bot.message_handler(commands=["privacy_profile_de"])
def _cmd_priv_de(m):
    with CFG_LOCK:
        CFG.region = "DE"
        CFG.privacy_mode = True
        CFG.show_platform = False
    save_cfg()
    bot.reply_to(
        m,
        (
            "Приватный профиль для DE применён:\n"
            "- Платформы скрыты в алертах (можно включить /show_platform_on).\n"
            "- Рекомендации: держите отдельные кошельки для DEX (Solana/BSC),\n"
            "  используйте проверенных провайдеров VPN, включайте split tunneling\n"
            "  так, чтобы приложения банков/идентификации работали вне VPN.\n"
            "- Соблюдайте местные законы и требования бирж/KYC."
        ),
    )


@bot.message_handler(commands=["p2p_tips"])
def _cmd_p2p_tips(m):
    bot.reply_to(
        m,
        (
            "P2P базовые советы (без обхода законов):\n"
            "• Разделяйте торговые и бытовые счета/кошельки.\n"
            "• Фиксируйте лимиты и комиссии, избегайте подозрительных премий.\n"
            "• Проверяйте репутацию контрагента, используйте escrow и 2FA.\n"
            "• Держите журнал сделок (дату/время/сумму/курс)."
        ),
    )


@bot.message_handler(commands=["privacy_tips"])
def _cmd_privacy_tips(m):
    bot.reply_to(
        m,
        (
            "Приватность (легальные практики):\n"
            "• VPN от проверенных вендоров, протоколы: WireGuard/OpenVPN.\n"
            "• Split tunneling: биржи/банки — вне VPN при необходимости.\n"
            "• Уникальные пароли, менеджер паролей, 2FA (TOTP/аппаратный).\n"
            "• Раздельные кошельки/браузерные профили для DEX и CEX."
        ),
    )


# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    load_cfg()
    set_webhook_once()
    print(">>> [webhook] starting Flask on 0.0.0.0:%d" % PORT)
    app.run(host="0.0.0.0", port=PORT)
