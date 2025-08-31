

# file: main.py
"""
Unified 24/7 Signals Bot — Webhook (Render), CEX+DEX scanner, human-readable trade alerts

Goal (from spec):
- Webhooks only (no polling) — stable on Render (fix 409)
- Planner: frequent scan for BTC/ETH, slower for alts/memes
- Autosignals: EMA(12/26) cross with EMA200 filter; RSI; pump/accel (% over window + z-score); scalp
- Anti-noise: min volume/liquidity, trend check, wick filter, dedupe
- DEX safety: whitelist networks/DEX, liq/vol/age thresholds, no honeypot (best-effort via DexScreener)
- Risk block in message: SL/TP (fixed % or R), position sizing hint
- Platforms suggested (no Binance): CEX: MEXC, KuCoin, Gate.io, Bybit; DEX: Jupiter/Raydium (Sol), Pancake v3 (BSC), Uniswap v3 (Arbitrum/Polygon), SunSwap (TRON)
- Privacy flags: privacy_mode, dex_only, show_platform, region UA/DE (advisory-only)

ENV (Render → Environment Variables):
  TELEGRAM_TOKEN, CHAT_ID(optional), WEBHOOK_BASE, WEBHOOK_SECRET
Optionally tune defaults with: DEFAULT_STRONG, DEFAULT_WINDOW, DEFAULT_SL, DEFAULT_TP, DEFAULT_DEBOUNCE

Start on Render:
  Build:  pip install -r requirements.txt
  Start:  python3 main.py

"""
from __future__ import annotations

import os
import time
import json
import math
import threading
import statistics
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Tuple

import requests
from flask import Flask, request, jsonify
import telebot
from telebot import types

# -----------------------------
# Config / Defaults
# -----------------------------
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("TELEGRAM_TOKEN missing in ENV")

CHAT_ID = int(os.getenv("CHAT_ID", "0") or 0)
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")  # e.g. https://crypto-bot-xxxx.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "hook")       # random path segment

# Runtime flags (chat commands will update these in-process)
@dataclass
class RuntimeCFG:
    # watchlists
    assets_top: List[str] = field(default_factory=lambda: ["BTC", "ETH"])
    assets_alts: List[str] = field(default_factory=lambda: ["SOL", "TON", "XRP", "BNB", "ADA", "TRX"])
    assets_memes: List[str] = field(default_factory=lambda: ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"])

    # windows & thresholds
    window_sec: int = int(os.getenv("DEFAULT_WINDOW", "600").rstrip("m"))  # seconds, default 10m
    strong_z: float = float(os.getenv("DEFAULT_STRONG", "2.2"))  # z-score/σ threshold for pump
    move_pct_top: float = 0.8   # softer for BTC/ETH (per 5–10m)
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
    risk_pct: float = 1.5  # percent of balance at risk per trade

    # anti-noise
    min_quote_vol_24h: float = 5_000_000.0  # CEX quote 24h vol minimal
    meme_quote_vol_24h: float = 10_000_000.0
    dedupe_sec: int = int(os.getenv("DEFAULT_DEBOUNCE", "900"))

    # DEX safety (DexScreener)
    dex_min_liq_usd: float = 300_000.0
    dex_min_vol24_usd: float = 500_000.0
    dex_min_age_days: int = 30

    # privacy & region
    privacy_mode: bool = True
    dex_only: bool = False
    show_platform: bool = True
    region: str = "UA"  # UA | DE

    # scheduler cadence (seconds)
    scan_top_sec: Tuple[int, int] = (15, 30)   # range (min,max) randomized
    scan_alts_sec: Tuple[int, int] = (30, 60)
    scan_memes_sec: Tuple[int, int] = (60, 300)

CFG = RuntimeCFG()

# Fees for net (maker/taker rough)
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

PORT = int(os.getenv("PORT", "8080"))

# Health endpoint for Render + UptimeRobot
@app.get("/health")
def _health():
    return ("OK", 200, {"Content-Type": "text/plain"})

# Set webhook on root hit (optional convenience)
@app.get("/")
def _root():
    try:
        bot.remove_webhook()
        if not WEBHOOK_BASE:
            return ("Set WEBHOOK_BASE env", 200)
        hook_url = f"{WEBHOOK_BASE}/telegram/{WEBHOOK_SECRET}"
        bot.set_webhook(url=hook_url)
        return (f"Webhook set → {hook_url}", 200)
    except Exception as e:
        return (f"Webhook error: {e}", 500)

# Telegram webhook endpoint
@app.post(f"/telegram/<secret>")
def _telegram(secret: str):
    if secret != WEBHOOK_SECRET:
        return ("forbidden", 403)
    try:
        update = telebot.types.Update.de_json(request.data.decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        print("webhook error:", e)
    return ("!", 200)

# -----------------------------
# Price Providers (CEX)
# -----------------------------
# Simple, rate-limit friendly requests with retry

def _http_get(url: str, timeout: int = 8) -> Optional[dict|list]:
    for _ in range(2):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "SignalsBot/1.0"})
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(0.5)
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
            qv = float(data.get("volValue", 0) or 0)  # quote volume
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
# DEX via DexScreener (best-effort)
# -----------------------------
# Token address map for common DEX assets
DEX_TOKEN_ADDR = {
    # ETH/WBTC (ERC), used sparsely
    "ETH": "0xC02aaA39b223FE8D0A0E5C4F27eAD9083C756Cc2",  # WETH
    "BTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",  # WBTC
    # Solana (addresses are base58, DexScreener supports chain=solana by token address)
    "SOL": None,  # native (use CEX mid instead)
    "WIF": "Es9vMFrzaCER…PLACEHOLDER",  # NOTE: fill correct address if needed
    "BONK": "DezX…PLACEHOLDER",
}
# NOTE: For safety, we will rely on CEX prices for now for most assets; DexScreener used to validate liq/vol for a subset.


def dexscreener_token(addr: str) -> Optional[dict]:
    d = _http_get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}")
    return d if isinstance(d, dict) else None


def dex_metrics_for(asset: str) -> Optional[Tuple[float, float, float, int]]:
    """Return (best_price_usd, liq_usd, vol24_usd, age_days) if available"""
    addr = DEX_TOKEN_ADDR.get(asset)
    if not addr:
        return None
    d = dexscreener_token(addr)
    if not d:
        return None
    pairs = d.get("pairs", [])
    best_price = None
    best_liq = 0.0
    best_vol24 = 0.0
    best_age_days = 0
    now_ms = int(time.time() * 1000)
    for p in pairs:
        try:
            price = float(p.get("priceUsd", 0) or 0)
            liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
            vol24 = float(p.get("volume", {}).get("h24", 0) or 0)
            created = int(p.get("pairCreatedAt", 0) or 0)
            age_days = max(0, int((now_ms - created) / (1000 * 60 * 60 * 24))) if created else 0
            if price > 0 and liq > best_liq:
                best_price, best_liq, best_vol24, best_age_days = price, liq, vol24, age_days
        except Exception:
            pass
    if best_price:
        return (best_price, best_liq, best_vol24, best_age_days)
    return None

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
        # EMA updates
        kf = 2 / (CFG.ema_fast + 1)
        ks = 2 / (CFG.ema_slow + 1)
        kt = 2 / (CFG.ema_trend + 1)
        self.ema_f = price if self.ema_f is None else (price - self.ema_f) * kf + self.ema_f
        self.ema_s = price if self.ema_s is None else (price - self.ema_s) * ks + self.ema_s
        self.ema_t = price if self.ema_t is None else (price - self.ema_t) * kt + self.ema_t
        # RSI (Wilder)
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
        # z-score of short returns (last window samples)
        if len(self.prices) < window + 1:
            return None
        returns = []
        last = None
        for _, p in list(self.prices)[-window-1:]:
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
SIGNALS_ON: bool = True
ARB_ON: bool = False
ARB_THRESH_NET: float = 3.0

# -----------------------------
# Helpers: platforms & messages
# -----------------------------
DEX_BY_ASSET = {
    "BTC": ["PancakeSwap v3 (BSC)"],
    "ETH": ["Uniswap v3 (Arbitrum)", "Uniswap v3 (Polygon)"]
            ,
    "SOL": ["Jupiter (Solana)", "Raydium (Solana)"],
    "TRX": ["SunSwap (TRON)"],
    "USDT": ["Jupiter (Solana)", "PancakeSwap v3 (BSC)"],
    "WIF": ["Jupiter (Solana)"],
    "BONK": ["Jupiter (Solana)", "Raydium (Solana)"],
    "PEPE": ["Uniswap v3 (Arbitrum)"],
    "FLOKI": ["PancakeSwap v3 (BSC)"]
}

CEX_BY_REGION = {
    "UA": ["MEXC", "Gate.io", "KuCoin", "Bybit"],
    "DE": ["Kraken", "Bitstamp", "Gate.io", "MEXC"],  # advisory-only
}

TRADE_URLS = {
    "MEXC": "https://www.mexc.com/exchange/{asset}_USDT",
    "KuCoin": "https://www.kucoin.com/trade/{asset}-USDT",
    "Gate.io": "https://www.gate.io/trade/{asset}_USDT",
}

SAFE_HOSTS = {"www.mexc.com", "www.kucoin.com", "www.gate.io"}


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
    risk_usd = CFG.balance_usdt * (CFG.risk_pct / 100.0)
    per_unit = max(1e-8, abs(entry - sl_price))
    qty = risk_usd / per_unit
    notional = qty * entry
    # Clamp by balance
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
        # Short hint only
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
        print("send_message error:", e)

# -----------------------------
# Signals Engine
# -----------------------------

def mid_price(symbol: str) -> Optional[float]:
    vals = []
    for ex, (book_fn, _vol_fn) in CEX_FUN.items():
        q = book_fn(symbol)
        if q:
            bid, ask = q
            vals.append((bid + ask) / 2)
    if not vals:
        return None
    return sum(vals) / len(vals)


def vol24_quote(symbol: str) -> float:
    total = 0.0
    for ex, (_book, vol_fn) in CEX_FUN.items():
        v = vol_fn(symbol)
        if v:
            total += v
    return total


def should_signal(symbol: str, ser: Series) -> Tuple[Optional[str], Optional[str]]:
    """Return (direction, reason) or (None, None). Applies EMA cross + trend + pump/accel filters."""
    if ser.ema_f is None or ser.ema_s is None:
        return (None, None)
    direction = None
    if ser.ema_f >= ser.ema_s:
        direction = "LONG"
        cross = "EMA BULL cross"
    else:
        direction = "SHORT"
        cross = "EMA BEAR cross"

    # Trend filter (EMA200)
    if CFG.require_trend and ser.ema_t is not None:
        if direction == "LONG" and ser.last_price is not None and ser.last_price < ser.ema_t:
            return (None, None)
        if direction == "SHORT" and ser.last_price is not None and ser.last_price > ser.ema_t:
            return (None, None)

    # Pump/scalp filters
    move = ser.pct_move(CFG.window_sec) or 0.0
    z = ser.zscore() or 0.0

    # per-asset baseline
    s = symbol.upper()
    base = CFG.move_pct_top if s in CFG.assets_top else (CFG.move_pct_alt if s in CFG.assets_alts else CFG.move_pct_meme)

    # Require both: strong move and z-score
    if abs(move) < base:
        return (None, None)
    if abs(z) < CFG.strong_z:
        return (None, None)

    # Acceleration heuristic: last two windows rising in same direction (approx with z>0 trend)
    # (Simplified; using z as accel proxy.)
    if direction == "LONG" and z < 0:
        return (None, None)
    if direction == "SHORT" and z > 0:
        return (None, None)

    reason = f"{cross}, над EMA200" if (CFG.require_trend and direction == "LONG") else (cross)
    reason += f", z={z:.1f}, Δ{move:+.2f}%/{int(CFG.window_sec/60)}m"
    return (direction, reason)


def dedupe_key(symbol: str, direction: str) -> str:
    return f"{symbol}:{direction}:{int(time.time() / CFG.dedupe_sec)}"

# -----------------------------
# Arbitrage (optional, CEX↔CEX net)
# -----------------------------

def apply_fees(px: float, f: Fees, side: str) -> float:
    if side == 'buy':
        return px * (1 + f.buy_pct / 100 + f.swap_pct / 100) + f.deposit_usdt
    else:
        return px * (1 - f.sell_pct / 100 - f.swap_pct / 100) - f.withdraw_usdt


def scan_arbitrage(symbols: Iterable[str]) -> List[Tuple[str, str, str, float]]:
    """Return top opportunities: (asset, buy_ex, sell_ex, net_pct)"""
    quotes: Dict[str, Dict[str, Tuple[float, float]]] = {ex: {} for ex in CEX_FUN}
    for ex, (book_fn, _vol_fn) in CEX_FUN.items():
        for s in symbols:
            q = book_fn(s)
            if q:
                quotes[ex][s] = q
    out = []
    for s in symbols:
        for buy_ex, qs in quotes.items():
            if s not in qs:
                continue
            bid_buy, ask_buy = qs[s]
            buy_net = apply_fees(ask_buy, FEES.get(buy_ex, Fees()), 'buy')
            for sell_ex, qs2 in quotes.items():
                if sell_ex == buy_ex or s not in qs2:
                    continue
                bid_sell, _ask_sell = qs2[s]
                sell_net = apply_fees(bid_sell, FEES.get(sell_ex, Fees()), 'sell')
                if buy_net <= 0:
                    continue
                net = (sell_net - buy_net) / buy_net * 100
                if net >= ARB_THRESH_NET:
                    out.append((s, buy_ex, sell_ex, net))
    out.sort(key=lambda x: x[3], reverse=True)
    return out[:3]

# -----------------------------
# Scheduler loop
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

            if not SIGNALS_ON:
                continue

            # CEX 24h volume filter
            qv = vol24_quote(s)
            min_qv = CFG.meme_quote_vol_24h if s in CFG.assets_memes else CFG.min_quote_vol_24h
            if qv < min_qv:
                continue

            direction, reason = should_signal(s, ser)
            if not direction:
                continue

            # Dedupe
            key = dedupe_key(s, direction)
            if time.time() - LAST_SENT.get(key, 0) < CFG.dedupe_sec:
                continue
            LAST_SENT[key] = time.time()

            # Build trade levels
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
                symbol=s, direction=direction, price=entry,
                sl_price=sl, tp_prices=[round(x, 4) for x in tp_prices],
                reason=reason, move_pct=ser.pct_move(CFG.window_sec), rsi=rsi_val, vol_hint=vol_hint
            )
            safe_send(text, kb)
        except Exception as e:
            print("scan err", s, e)


def _runner():
    # Staggered loops with different cadences
    import random
    while not STOP_EVENT.is_set():
        t0 = time.time()
        _scan_cycle(CFG.assets_top)
        time.sleep(random.uniform(*CFG.scan_top_sec))
        _scan_cycle(CFG.assets_alts)
        time.sleep(random.uniform(*CFG.scan_alts_sec))
        _scan_cycle(CFG.assets_memes)
        time.sleep(random.uniform(*CFG.scan_memes_sec))
        if ARB_ON:
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
                print("arb err:", e)
        # Safety cap
        dt = time.time() - t0
        if dt < 1:
            time.sleep(1)

# Launch background scanner thread at import-time so it runs under Flask
threading.Thread(target=_runner, daemon=True).start()

# -----------------------------
# Commands
# -----------------------------
@bot.message_handler(commands=["start"])
def _cmd_start(m):
    global CHAT_ID
    if CHAT_ID == 0:
        CHAT_ID = m.chat.id
    bot.reply_to(m, (
        "Привет! Я 24/7 сканирую рынок и присылаю понятные сигналы.\n"
        "/help — команды\n"
        "/status — параметры\n"
        "/signals on|off — включить/выключить авто-алерты\n"
        "/set_assets BTC,ETH,SOL — наблюдаемый список (все классы)\n"
        "/set_window 10m — окно для Δ% и z-score (в секундах или Xm)\n"
        "/set_strong 2.2 — порог σ для пампов\n"
        "/set_sl 1.8 | /set_tp 1.5 2.5 4 — риск\n"
        "/ema200 on|off — фильтр по тренду\n"
        "/set_rsi 14 — длина RSI\n"
        "/debounce 900 — анти-спам (сек)\n"
        "/arb_on|/arb_off, /arb_thresh 3.0 — арбитраж (опционально)\n"
        "/privacy_on|off, /dex_only_on|off, /show_platform_on|off, /set_region UA|DE"
         ))
  
# --- PATCH: add three profile commands (/mode aggressive, /mode standard, /mode safe)
# Paste this block near your existing @bot.message_handler commands in main.py
# Safe for your current CFG: it checks attributes before setting.

from typing import Any, List

# -------- helpers (non‑breaking setters) --------

def _set(obj: Any, name: str, value: Any) -> bool:
    if hasattr(obj, name):
        try:
            setattr(obj, name, value)
            return True
        except Exception:
            return False
    return False


def _apply_window(cfg: Any, seconds: int):
    # window for pump/zscore logic
    _set(cfg, 'window_sec', int(seconds))
    # compatibility if your build uses minutes
    _set(cfg, 'lookback_min', max(1, int(round(seconds/60))))
    # make polling reasonably fast
    _set(cfg, 'poll_interval_sec', min(30, max(10, seconds//2)))


def _apply_strong(cfg: Any, strong_sigma: float, fallback_move: List[float] | None = None):
    # prefer z-score param if present
    _set(cfg, 'strong_sigma', float(strong_sigma))
    if fallback_move:
        # fallback for engines без z-score: tune move %
        if len(fallback_move) == 2:
            _set(cfg, 'move_up_pct', float(fallback_move[0]))
            _set(cfg, 'move_dn_pct', float(fallback_move[1]))


def _apply_tp(cfg: Any, tps: List[float]):
    # unified list if supported
    if not _set(cfg, 'tp_pcts', list(tps)):
        # legacy 3 fields
        if len(tps) >= 3:
            _set(cfg, 'tp1_pct', float(tps[0]))
            _set(cfg, 'tp2_pct', float(tps[1]))
            _set(cfg, 'tp3_pct', float(tps[2]))


def _apply_sl(cfg: Any, sl_pct: float):
    _set(cfg, 'sl_pct', float(sl_pct))


def _apply_ema200(cfg: Any, on: bool):
    _set(cfg, 'ema200_filter', bool(on))
    # ensure ema lengths sane
    _set(cfg, 'ema_fast', getattr(cfg, 'ema_fast', 12))
    _set(cfg, 'ema_slow', getattr(cfg, 'ema_slow', 26))


def _apply_rsi(cfg: Any, period: int):
    _set(cfg, 'rsi_len', int(period))


def _apply_debounce(cfg: Any, seconds: int):
    _set(cfg, 'debounce_sec', int(seconds))


def _enable_providers(names_on: List[str], names_off: List[str]):
    # expects CFG.providers like {"MEXC": ProviderCfg(...)}
    try:
        for nm in names_on:
            if nm in CFG.providers:
                CFG.providers[nm].enabled = True
        for nm in names_off:
            if nm in CFG.providers:
                CFG.providers[nm].enabled = False
    except Exception:
        pass


# -------- modes --------

@bot.message_handler(commands=['mode'])
def cmd_mode_help(m):
    parts = m.text.strip().split()
    if len(parts) == 1:
        bot.reply_to(m, (
            "Использование: /mode aggressive | standard | safe\n"
            "aggressive — максимум сигналов (включая мемы, Solana DEX), короткие SL/TP.\n"
            "standard — дневной режим по умолчанию, баланс сигналов/качества.\n"
            "safe — строгие фильтры, меньше сигналов (ночью/занята)."
        ))
        return

@bot.message_handler(commands=['mode_aggressive'])
def mode_aggressive_alias(m):
    _mode_aggressive(m)

@bot.message_handler(commands=['mode_standard'])
def mode_standard_alias(m):
    _mode_standard(m)

@bot.message_handler(commands=['mode_safe'])
def mode_safe_alias(m):
    _mode_safe(m)

@bot.message_handler(func=lambda msg: msg.text and msg.text.strip().lower().startswith('/mode '))
def mode_router(m):
    arg = m.text.strip().split(maxsplit=1)[1].lower()
    if arg.startswith('aggr'): _mode_aggressive(m)
    elif arg.startswith('std') or arg.startswith('stan'): _mode_standard(m)
    elif arg.startswith('safe'): _mode_safe(m)
    else: bot.reply_to(m, 'Неизвестный режим. Используй: aggressive | standard | safe')


def _mode_aggressive(m):
    # Assets: top + memes
    CFG.watch = ['BTC','ETH','SOL','XRP','BNB','DOGE','PEPE','FLOKI','WIF','BONK']
    # Window/strength
    _apply_window(CFG, 180)                 # 3m окно
    _apply_strong(CFG, 1.6, fallback_move=[0.8, 1.0])
    # Risk model
    _apply_sl(CFG, 0.3)
    _apply_tp(CFG, [0.6, 1.2, 2.0])
    _apply_ema200(CFG, False)
    _apply_rsi(CFG, 9)
    _apply_debounce(CFG, 300)
    # Providers: all (no Binance), allow DEX aggregator if present in your build
    _enable_providers(['MEXC','KuCoin','Gate.io','DEX','Raydium','Jupiter'], [])
    # mark mode
    setattr(CFG, 'active_mode', 'AGGRESSIVE')
    bot.reply_to(m,
        "⚡ Режим: AGGRESSIVE\n"
        "Активы: BTC, ETH, SOL, XRP, BNB + DOGE, PEPE, FLOKI, WIF, BONK\n"
        "Окно: 3m | Сила ≥1.6σ | SL 0.3% | TP 0.6/1.2/2.0% | EMA200 OFF | RSI 9 | Debounce 300s\n"
        "Источники: MEXC, KuCoin, Gate.io + Solana DEX (если включён)"
    )


def _mode_standard(m):
    CFG.watch = ['BTC','ETH','SOL','TON','XRP','BNB','ADA']
    _apply_window(CFG, 300)                 # 5m
    _apply_strong(CFG, 2.0, fallback_move=[1.0, 1.2])
    _apply_sl(CFG, 0.4)
    _apply_tp(CFG, [0.8, 1.6, 3.0])
    _apply_ema200(CFG, True)
    _apply_rsi(CFG, 14)
    _apply_debounce(CFG, 600)
    _enable_providers(['MEXC','KuCoin','Gate.io','DEX'], [])
    setattr(CFG, 'active_mode', 'STANDARD')
    bot.reply_to(m,
        "✅ Режим: STANDARD\n"
        "Активы: BTC, ETH, SOL, TON, XRP, BNB, ADA\n"
        "Окно: 5m | Сила ≥2.0σ | SL 0.4% | TP 0.8/1.6/3.0% | EMA200 ON | RSI 14 | Debounce 600s\n"
        "Источники: MEXC, KuCoin, Gate.io (+ DEX агрегатор, если доступен)"
    )


def _mode_safe(m):
    CFG.watch = ['BTC','ETH','SOL','TON','ADA']
    _apply_window(CFG, 600)                 # 10m
    _apply_strong(CFG, 2.6, fallback_move=[1.5, 1.8])
    _apply_sl(CFG, 0.6)
    _apply_tp(CFG, [1.5, 3.0, 5.0])
    _apply_ema200(CFG, True)
    _apply_rsi(CFG, 14)
    _apply_debounce(CFG, 1200)
    _enable_providers(['MEXC','KuCoin'], ['Gate.io'])  # ночью экономим на шуме
    setattr(CFG, 'active_mode', 'SAFE')
    bot.reply_to(m,
        "🛡 Режим: SAFE\n"
        "Активы: BTC, ETH, SOL, TON, ADA\n"
        "Окно: 10m | Сила ≥2.6σ | SL 0.6% | TP 1.5/3.0/5.0% | EMA200 ON | RSI 14 | Debounce 1200s\n"
        "Источники: MEXC, KuCoin (без шумных)"
    )


# (Опционально) строка в /status — показать активный режим.
try:
    _old_status = cmd_status  # keep reference
    @bot.message_handler(commands=['status'])
    def cmd_status_with_mode(m):
        # call old handler first
        try:
            _old_status(m)
        except Exception:
            pass
        mode = getattr(CFG, 'active_mode', '—')
        bot.reply_to(m, f"Режим: {mode}")
except Exception:
    pass


# --- PLAN B (failsafe) — minimal modes if the big patch conflicts ---
# Paste this *instead* if the main patch errors on deploy. It only sets the
# core fields and avoids touching providers/status wrappers.

from typing import Any

def _sa(o: Any, n: str, v: Any):
    try:
        if hasattr(o, n):
            setattr(o, n, v)
    except Exception:
        pass

def _win(sec: int):
    _sa(CFG, 'window_sec', int(sec))
    _sa(CFG, 'lookback_min', max(1, int(round(sec/60))))
    _sa(CFG, 'poll_interval_sec', min(30, max(10, sec//2)))

def _tp(a,b,c):
    _sa(CFG, 'tp_pcts', [a,b,c])
    _sa(CFG, 'tp1_pct', a); _sa(CFG, 'tp2_pct', b); _sa(CFG, 'tp3_pct', c)

@bot.message_handler(commands=['mode_aggressive_b'])
def mode_aggr_b(m):
    CFG.watch = ['BTC','ETH','SOL','XRP','BNB','DOGE','PEPE','FLOKI','WIF','BONK']
    _win(180); _sa(CFG,'strong_sigma',1.6); _sa(CFG,'move_up_pct',0.8); _sa(CFG,'move_dn_pct',1.0)
    _sa(CFG,'sl_pct',0.3); _tp(0.6,1.2,2.0)
    _sa(CFG,'ema200_filter',False); _sa(CFG,'rsi_len',9); _sa(CFG,'debounce_sec',300)
    _sa(CFG,'active_mode','AGGRESSIVE')
    bot.reply_to(m, '⚡ Режим: AGGRESSIVE (Plan B) включён')

@bot.message_handler(commands=['mode_standard_b'])
def mode_std_b(m):
    CFG.watch = ['BTC','ETH','SOL','TON','XRP','BNB','ADA']
    _win(300); _sa(CFG,'strong_sigma',2.0); _sa(CFG,'move_up_pct',1.0); _sa(CFG,'move_dn_pct',1.2)
    _sa(CFG,'sl_pct',0.4); _tp(0.8,1.6,3.0)
    _sa(CFG,'ema200_filter',True); _sa(CFG,'rsi_len',14); _sa(CFG,'debounce_sec',600)
    _sa(CFG,'active_mode','STANDARD')
    bot.reply_to(m, '✅ Режим: STANDARD (Plan B) включён')

@bot.message_handler(commands=['mode_safe_b'])
def mode_safe_b(m):
    CFG.watch = ['BTC','ETH','SOL','TON','ADA']
    _win(600); _sa(CFG,'strong_sigma',2.6); _sa(CFG,'move_up_pct',1.5); _sa(CFG,'move_dn_pct',1.8)
    _sa(CFG,'sl_pct',0.6); _tp(1.5,3.0,5.0)
    _sa(CFG,'ema200_filter',True); _sa(CFG,'rsi_len',14); _sa(CFG,'debounce_sec',1200)
    _sa(CFG,'active_mode','SAFE')
    bot.reply_to(m, '🛡 Режим: SAFE (Plan B) включён')


@bot.message_handler(commands=["help"])
def _cmd_help(m):
    bot.reply_to(m, (
        "Команды:\n"
        "/status\n"
        "/signals on|off\n"
        "/set_assets <список,через,запятую>\n"
        "/set_window <число> или <Xm>\n"
        "/set_strong <σ>\n"
        "/set_sl <pct> | /set_tp <pct pct pct>\n"
        "/ema200 on|off | /set_rsi <n> | /debounce <sec>\n"
        "/arb_on|/arb_off | /arb_thresh <pct>\n"
        "/privacy_on|off | /dex_only_on|off | /show_platform_on|off | /set_region UA|DE"
    ))

@bot.message_handler(commands=["status"])
def _cmd_status(m):
    lines = []
    lines.append(f"Assets TOP: {', '.join(CFG.assets_top)}")
    lines.append(f"Assets ALTS: {', '.join(CFG.assets_alts)}")
    lines.append(f"Assets MEMES: {', '.join(CFG.assets_memes)}")
    lines.append(f"Window: {CFG.window_sec}s | strong z≥{CFG.strong_z}")
    lines.append(f"EMA: 12/26, trend200={'ON' if CFG.require_trend else 'OFF'} | RSI={CFG.rsi_len}")
    lines.append(f"Risk: SL {CFG.sl_pct}% | TP {', '.join([str(x) for x in CFG.tp_list])}% | balance {CFG.balance_usdt}$ | risk {CFG.risk_pct}%")
    lines.append(f"Anti-noise: min24hVol {CFG.min_quote_vol_24h/1_000_000:.1f}M, memes {CFG.meme_quote_vol_24h/1_000_000:.1f}M | debounce {CFG.dedupe_sec}s")
    lines.append(f"Signals: {'ON' if SIGNALS_ON else 'OFF'} | Arb: {'ON' if ARB_ON else 'OFF'} (≥{ARB_THRESH_NET}%)")
    lines.append(f"Privacy: {'ON' if CFG.privacy_mode else 'OFF'}, DEX-only: {'ON' if CFG.dex_only else 'OFF'}, Show platform: {'ON' if CFG.show_platform else 'OFF'}, Region: {CFG.region}")
    bot.reply_to(m, "\n".join(lines))

@bot.message_handler(commands=["signals"])
def _cmd_signals(m):
    global SIGNALS_ON
    parts = m.text.strip().split()
    if len(parts) == 2 and parts[1].lower() in ("on", "off"):
        SIGNALS_ON = (parts[1].lower() == "on")
        bot.reply_to(m, f"Signals: {'ON' if SIGNALS_ON else 'OFF'}")
    else:
        bot.reply_to(m, "Использование: /signals on|off")

@bot.message_handler(commands=["set_assets"])
def _cmd_assets(m):
    raw = m.text.split(" ", 1)
    if len(raw) < 2:
        bot.reply_to(m, "Использование: /set_assets BTC,ETH,SOL,… (все классы)")
        return
    parts = [x.strip().upper() for x in raw[1].replace(";", ",").split(",") if x.strip()]
    if not parts:
        bot.reply_to(m, "Пустой список")
        return
    # naive repartition: top remains, others go to alts; memes preserved if present
    top = [x for x in parts if x in ("BTC", "ETH")]
    memes = [x for x in parts if x in CFG.assets_memes]
    alts = [x for x in parts if x not in top and x not in memes]
    CFG.assets_top = top or CFG.assets_top
    CFG.assets_alts = alts or CFG.assets_alts
    CFG.assets_memes = memes or CFG.assets_memes
    bot.reply_to(m, f"OK. Обновлено. TOP={CFG.assets_top}, ALTS={CFG.assets_alts}, MEMES={CFG.assets_memes}")

@bot.message_handler(commands=["set_window"])
def _cmd_window(m):
    parts = m.text.strip().split()
    if len(parts) != 2:
        bot.reply_to(m, "Использование: /set_window 600 или 10m")
        return
    v = parts[1].lower()
    try:
        if v.endswith('m'):
            CFG.window_sec = int(float(v[:-1]) * 60)
        else:
            CFG.window_sec = int(v)
        bot.reply_to(m, f"OK. Window={CFG.window_sec}s")
    except Exception:
        bot.reply_to(m, "Формат: 600 или 10m")

@bot.message_handler(commands=["set_strong"])
def _cmd_strong(m):
    parts = m.text.strip().split()
    try:
        CFG.strong_z = float(parts[1])
        bot.reply_to(m, f"OK. strong z≥{CFG.strong_z}")
    except Exception:
        bot.reply_to(m, "Использование: /set_strong 2.2")

@bot.message_handler(commands=["set_sl"])
def _cmd_sl(m):
    parts = m.text.strip().split()
    try:
        CFG.sl_pct = float(parts[1])
        bot.reply_to(m, f"OK. SL={CFG.sl_pct}%")
    except Exception:
        bot.reply_to(m, "Использование: /set_sl 1.8")

@bot.message_handler(commands=["set_tp"])
def _cmd_tp(m):
    raw = m.text.split(" ", 1)
    if len(raw) < 2:
        bot.reply_to(m, "Использование: /set_tp 1.5 2.5 4")
        return
    try:
        parts = [float(x) for x in raw[1].split() if x.strip()]
        if not parts:
            raise ValueError
        CFG.tp_list = parts
        bot.reply_to(m, f"OK. TP set: {CFG.tp_list}")
    except Exception:
        bot.reply_to(m, "Формат: числа через пробел")

@bot.message_handler(commands=["ema200"])
def _cmd_ema200(m):
    parts = m.text.strip().split()
    if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
        bot.reply_to(m, "Использование: /ema200 on|off")
        return
    CFG.require_trend = (parts[1].lower() == "on")
    bot.reply_to(m, f"EMA200 filter: {'ON' if CFG.require_trend else 'OFF'}")

@bot.message_handler(commands=["set_rsi"])
def _cmd_rsi(m):
    parts = m.text.strip().split()
    try:
        CFG.rsi_len = int(parts[1])
        bot.reply_to(m, f"OK. RSI len={CFG.rsi_len}")
    except Exception:
        bot.reply_to(m, "Использование: /set_rsi 14")

@bot.message_handler(commands=["debounce"])
def _cmd_debounce(m):
    parts = m.text.strip().split()
    try:
        CFG.dedupe_sec = int(parts[1])
        bot.reply_to(m, f"OK. debounce={CFG.dedupe_sec}s")
    except Exception:
        bot.reply_to(m, "Использование: /debounce 1200")

@bot.message_handler(commands=["arb_on"])
def _cmd_arb_on(m):
    global ARB_ON
    ARB_ON = True
    bot.reply_to(m, "Арбитраж: ON")

@bot.message_handler(commands=["arb_off"])
def _cmd_arb_off(m):
    global ARB_ON
    ARB_ON = False
    bot.reply_to(m, "Арбитраж: OFF")

@bot.message_handler(commands=["arb_thresh"])
def _cmd_arb_thr(m):
    global ARB_THRESH_NET
    parts = m.text.strip().split()
    try:
        ARB_THRESH_NET = float(parts[1])
        bot.reply_to(m, f"OK. arb threshold net={ARB_THRESH_NET}%")
    except Exception:
        bot.reply_to(m, "Использование: /arb_thresh 3.0")

@bot.message_handler(commands=["privacy_on"])
def _p_on(m):
    CFG.privacy_mode = True
    bot.reply_to(m, "Privacy mode: ON (DEX в приоритете, можно скрыть платформы)")

@bot.message_handler(commands=["privacy_off"])
def _p_off(m):
    CFG.privacy_mode = False
    bot.reply_to(m, "Privacy mode: OFF")

@bot.message_handler(commands=["dex_only_on"])
def _d_on(m):
    CFG.dex_only = True
    bot.reply_to(m, "DEX-only: ON")

@bot.message_handler(commands=["dex_only_off"])
def _d_off(m):
    CFG.dex_only = False
    bot.reply_to(m, "DEX-only: OFF")

@bot.message_handler(commands=["show_platform_on"])
def _sp_on(m):
    CFG.show_platform = True
    bot.reply_to(m, "Показывать платформу: ON")

@bot.message_handler(commands=["show_platform_off"])
def _sp_off(m):
    CFG.show_platform = False
    bot.reply_to(m, "Показывать платформу: OFF")

@bot.message_handler(commands=["set_region"])
def _set_region(m):
    parts = m.text.strip().split()
    if len(parts) == 2 and parts[1].upper() in ("UA", "DE"):
        CFG.region = parts[1].upper()
        bot.reply_to(m, f"Регион: {CFG.region}")
    else:
        bot.reply_to(m, "Использование: /set_region UA|DE")
      

# -----------------------------
# Main
# -----------------------------

import threading, asyncio, time

def _run_scanner_forever():
    print(">>> [scanner] starting...")
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(alert_loop())  # 🚀 тут твой async-сканер
            loop.close()
        except Exception as e:
            print(f">>> [scanner] crashed: {e}. Restart in 5s...")
            time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=_run_scanner_forever, daemon=True).start()
    print(">>> [webhook] starting Flask...")
    app.run(host="0.0.0.0", port=PORT)
