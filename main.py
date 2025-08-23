# -*- coding: utf-8 -*-
# CryptoBella — 24/7 Signals Bot (CEX+DEX, scalp, EMA, arbitrage)
# Sources: MEXC, KuCoin, Gate.io (+ Uniswap via Dexscreener for ETH/BTC)
# Hosting: любой сервис, где можно выставить переменные среды:
#   TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (второе можно не задавать — бот привяжется по /start)
# Keepalive: aiohttp /health (удобно для аптайм‑монитора)

import os
import time
import math
import threading
import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional, List, Deque

import requests
import telebot
from aiohttp import web


# ====================== CONFIG ======================

@dataclass
class ProviderCfg:
    enabled: bool = True
    eta_min: int = 5  # примерное время перевода (минуты)

@dataclass
class Fees:
    buy_pct: float = 0.10   # комиссия покупки, %
    sell_pct: float = 0.10  # комиссия продажи, %
    swap_pct: float = 0.00  # своп (для DEX, по умолчанию 0 тут)
    dep_usdt: float = 0.0   # депо в USDT
    wd_usdt: float = 0.0    # вывод в USDT

@dataclass
class Config:
    # наблюдаемые активы
    watch: List[str] = field(default_factory=lambda: [
        "BTC","ETH","SOL","TON","BNB","XRP","ADA","TRX",
        "DOGE","SHIB","PEPE","FLOKI","WIF","BONK"
    ])

    # окно и пороги сигналов
    window_min: int = 10
    strong_move_pct: float = 2.2  # «сильный толчок»
    debounce_sec: int = 1200      # анти-спам

    # индикаторы
    ema_fast: int = 12
    ema_slow: int = 26
    ema200_on: bool = True
    rsi_period: int = 14
    rsi_hot: int = 68
    rsi_cold: int = 32

    # риск‑менеджмент
    sl_pct: float = 1.8
    tp_pcts: List[float] = field(default_factory=lambda: [1.5, 2.5, 4.0])

    # источники и комиссии
    providers: Dict[str, ProviderCfg] = field(default_factory=lambda: {
        "MEXC":   ProviderCfg(True, 5),
        "KuCoin": ProviderCfg(True, 5),
        "Gate.io":ProviderCfg(True, 6),
        "DEX":    ProviderCfg(True, 3),  # используется только для цены ETH/BTC
    })
    fees: Dict[str, Fees] = field(default_factory=lambda: {
        "MEXC":   Fees(0.10, 0.10),
        "KuCoin": Fees(0.10, 0.10),
        "Gate.io":Fees(0.20, 0.20),
        "DEX":    Fees(0.00, 0.00, swap_pct=0.30),
    })

    # арбитраж
    arb_on: bool = True
    arb_min_profit_pct: float = 3.0
    arb_max_eta: int = 20

CFG = Config()

# Dexscreener адреса (ETH=WETH, BTC=WBTC ERC20)
DEX_TOKEN_ADDR = {
    "ETH": "0xC02aaA39b223FE8D0A0E5C4F27eAD9083C756Cc2",  # WETH
    "BTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",  # WBTC
}
DEX_USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"

TRADE_URLS = {
    "MEXC":   "https://www.mexc.com/exchange/{asset}_USDT",
    "KuCoin": "https://www.kucoin.com/trade/{asset}-USDT",
    "Gate.io":"https://www.gate.io/trade/{asset}_USDT",
}
SAFE_HOSTS = {"www.mexc.com","www.kucoin.com","www.gate.io"}

# токен/чат
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0") or 0)
if not TOKEN:
    print("❌ TELEGRAM_TOKEN отсутствует в переменных среды")
    raise SystemExit(1)

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ====================== PROVIDERS ======================

def mexc(symbol: str) -> Optional[Tuple[float, float]]:
    url = f"https://api.mexc.com/api/v3/ticker/bookTicker?symbol={symbol}USDT"
    try:
        d = requests.get(url, timeout=6).json()
        bid = float(d.get("bidPrice", 0) or 0)
        ask = float(d.get("askPrice", 0) or 0)
        return (bid, ask) if bid > 0 and ask > 0 else None
    except Exception:
        return None

def kucoin(symbol: str) -> Optional[Tuple[float, float]]:
    url = f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}-USDT"
    try:
        d = requests.get(url, timeout=6).json()
        if d.get("code") != "200000":
            return None
        data = d.get("data", {})
        bid = float(data.get("bestBidPrice", data.get("bestBid", 0)) or 0)
        ask = float(data.get("bestAskPrice", data.get("bestAsk", 0)) or 0)
        return (bid, ask) if bid > 0 and ask > 0 else None
    except Exception:
        return None

def gate(symbol: str) -> Optional[Tuple[float, float]]:
    url = f"https://api.gateio.ws/api/v4/spot/order_book?currency_pair={symbol}_USDT&limit=1"
    try:
        d = requests.get(url, timeout=6).json()
        bids = d.get("bids", [])
        asks = d.get("asks", [])
        bid = float(bids[0][0]) if bids else 0.0
        ask = float(asks[0][0]) if asks else 0.0
        return (bid, ask) if bid > 0 and ask > 0 else None
    except Exception:
        return None

PROVIDER_FUN = {
    "MEXC": mexc,
    "KuCoin": kucoin,
    "Gate.io": gate,
}

def dex_uniswap_usd(symbol: str) -> Optional[float]:
    addr = DEX_TOKEN_ADDR.get(symbol)
    if not addr:
        return None
    try:
        d = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=8).json()
        pairs = d.get("pairs", []) if isinstance(d, dict) else []
        best = None
        for p in pairs:
            if p.get("chainId") == "ethereum":
                q = p.get("quoteToken", {})
                if (q.get("address", "") or "").lower() == DEX_USDT.lower():
                    price = float(p.get("priceUsd", 0) or 0)
                    if price > 0:
                        best = price if best is None or price < best else best
        return best
    except Exception:
        return None

# ====================== SERIES / INDICATORS ======================

class Series:
    def __init__(self, rsi_period: int):
        self.buf: Deque[Tuple[float, float]] = deque(maxlen=5000)
        self.ema_f: Optional[float] = None
        self.ema_s: Optional[float] = None
        self.ema_200: Optional[float] = None
        self.last_cross: Optional[str] = None  # 'bull'|'bear'
        self.rsi_period = rsi_period
        self.rsi_prices: Deque[float] = deque(maxlen=rsi_period + 1)

    def add(self, price: float) -> None:
        ts = time.time()
        self.buf.append((ts, price))
        self.rsi_prices.append(price)

        kf = 2 / (CFG.ema_fast + 1)
        ks = 2 / (CFG.ema_slow + 1)
        k200 = 2 / (200 + 1)

        self.ema_f = price if self.ema_f is None else (price - self.ema_f) * kf + self.ema_f
        self.ema_s = price if self.ema_s is None else (price - self.ema_s) * ks + self.ema_s
        self.ema_200 = price if self.ema_200 is None else (price - self.ema_200) * k200 + self.ema_200

    def pct_move(self, minutes: int) -> Optional[float]:
        if not self.buf:
            return None
        cutoff = time.time() - minutes * 60
        base = None
        for ts, px in self.buf:
            if ts >= cutoff:
                base = px
                break
        if base is None:
            base = self.buf[0][1]
        last = self.buf[-1][1]
        return (last - base) / base * 100 if base > 0 else None

    def rsi(self) -> Optional[float]:
        if len(self.rsi_prices) < self.rsi_period + 1:
            return None
        gains = 0.0
        losses = 0.0
        prev = None
        for p in self.rsi_prices:
            if prev is not None:
                ch = p - prev
                if ch > 0:
                    gains += ch
                else:
                    losses += -ch
            prev = p
        avg_gain = gains / self.rsi_period
        avg_loss = losses / self.rsi_period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def cross(self) -> Optional[str]:
        if self.ema_f is None or self.ema_s is None:
            return None
        cur = 'bull' if self.ema_f >= self.ema_s else 'bear'
        if self.last_cross is None:
            self.last_cross = cur
            return None
        if cur != self.last_cross:
            self.last_cross = cur
            return cur
        return None


SERIES: Dict[str, Series] = {s: Series(CFG.rsi_period) for s in CFG.watch}
ALERTS_ON = True
LAST_SENT: Dict[str, float] = {}

# ====================== UTILS ======================

def provider_trade_url(ex: str, asset: str) -> Optional[str]:
    tpl = TRADE_URLS.get(ex)
    if not tpl:
        return None
    try:
        url = tpl.format(asset=asset)
        # для безопасности проверим хост
        from urllib.parse import urlparse
        if urlparse(url).netloc in SAFE_HOSTS:
            return url
    except Exception:
        pass
    return None

def mid_price(symbol: str) -> Optional[float]:
    vals: List[float] = []
    for ex, fn in PROVIDER_FUN.items():
        if not CFG.providers.get(ex, ProviderCfg()).enabled:
            continue
        q = fn(symbol)
        if q:
            bid, ask = q
            vals.append((bid + ask) / 2.0)
    if not vals:
        return None
    return sum(vals) / len(vals)

def best_entry_exchange(symbol: str, side: str) -> Optional[Tuple[str, float]]:
    """
    side: 'long' -> минимальный ask; 'short' -> максимальный bid (формально, для спота это ориентир)
    """
    best_ex = None
    best_px = None
    for ex, fn in PROVIDER_FUN.items():
        if not CFG.providers.get(ex, ProviderCfg()).enabled:
            continue
        q = fn(symbol)
        if not q:
            continue
        bid, ask = q
        if side == 'long':
            px = ask
            if best_px is None or px < best_px:
                best_px = px
                best_ex = ex
        else:
            px = bid
            if best_px is None or px > best_px:
                best_px = px
                best_ex = ex
    if best_ex is None or best_px is None:
        return None
    return best_ex, best_px

def apply_fees(px: float, f: Fees, side: str) -> float:
    if side == 'buy':
        return px * (1 + f.buy_pct / 100 + f.swap_pct / 100) + f.dep_usdt
    return px * (1 - f.sell_pct / 100 - f.swap_pct / 100) - f.wd_usdt

@dataclass
class Opp:
    asset: str
    buy_ex: str
    sell_ex: str
    buy_px: float
    sell_px: float
    net_pct: float
    eta: int

def compute_arbitrage(symbols: List[str]) -> List[Opp]:
    out: List[Opp] = []
    quotes: Dict[str, Dict[str, Tuple[float, float]]] = {ex: {} for ex in PROVIDER_FUN}
    for ex, fn in PROVIDER_FUN.items():
        if not CFG.providers.get(ex, ProviderCfg()).enabled:
            continue
        for s in symbols:
            q = fn(s)
            if q:
                quotes[ex][s] = q

    for s in symbols:
        for buy_ex in PROVIDER_FUN.keys():
            if s not in quotes.get(buy_ex, {}):
                continue
            for sell_ex in PROVIDER_FUN.keys():
                if sell_ex == buy_ex:
                    continue
                if s not in quotes.get(sell_ex, {}):
                    continue
                bid_buy, ask_buy = quotes[buy_ex][s]
                bid_sell, ask_sell = quotes[sell_ex][s]
                buy = apply_fees(ask_buy, CFG.fees.get(buy_ex, Fees()), 'buy')
                sell = apply_fees(bid_sell, CFG.fees.get(sell_ex, Fees()), 'sell')
                if buy <= 0:
                    continue
                net = (sell - buy) / buy * 100.0
                eta = CFG.providers.get(buy_ex, ProviderCfg()).eta_min + CFG.providers.get(sell_ex, ProviderCfg()).eta_min
                if net >= CFG.arb_min_profit_pct and eta <= CFG.arb_max_eta:
                    out.append(Opp(s, buy_ex, sell_ex, buy, sell, net, eta))
    out.sort(key=lambda x: x.net_pct, reverse=True)
    return out[:3]

def human_pct(x: float) -> str:
    s = f"{x:.2f}"
    s = s.replace(".00", "")
    return s

def safe_send(text: str, buttons: Optional[List[Tuple[str, str]]] = None) -> None:
    global CHAT_ID
    if CHAT_ID == 0:
        print("ℹ️ CHAT_ID не установлен. Отправьте /start в личный чат с ботом.")
        return
    markup = None
    if buttons:
        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = []
        row = []
        for label, url in buttons:
            if url:
                row.append(InlineKeyboardButton(text=label, url=url))
        if row:
            kb.append(row)
        if kb:
            markup = InlineKeyboardMarkup(kb)
    try:
        bot.send_message(CHAT_ID, text, disable_web_page_preview=True, reply_markup=markup)
    except Exception as e:
        print("send_message error:", e)

def send_scalp_signal(s: str, direction: str, px: float, pm: float, rsi_val: Optional[float]) -> None:
    pair = f"{s}/USDT"
    sl = CFG.sl_pct
    tps = ", ".join([f"{tp:.1f}%" for tp in CFG.tp_pcts])

    # биржа для входа
    best = best_entry_exchange(s, 'long' if direction == "LONG" else 'short')
    ex_suggest = best[0] if best else "MEXC / KuCoin / Gate.io"

    rsi_text = f"RSI={rsi_val:.1f}" if rsi_val is not None else "RSI=n/a"
    text = (
        f"🚀 <b>{s}</b> {('+' if pm>=0 else '')}{pm:.2f}% за {CFG.window_min} мин\n"
        f"{pair} ≈ <b>{px:,.6f}$</b>\n"
        f"EMA12 {'>' if direction=='LONG' else '<'} EMA26 | {rsi_text}\n"
        f"Направление: <b>{direction}</b>\n"
        f"Платформа: <b>{ex_suggest}</b>\n"
        f"Подсказка: SL {sl:.1f}%, частичный TP {tps}"
    )
    safe_send(text)

def send_ema_cross(s: str, kind: str, px: float) -> None:
    text = f"⚡️ <b>{s}</b> EMA {kind.upper()} cross | {px:,.2f}$"
    safe_send(text)

def send_arb(o: Opp) -> None:
    buttons = [
        (f"Купить на {o.buy_ex}", provider_trade_url(o.buy_ex, o.asset)),
        (f"Продать на {o.sell_ex}", provider_trade_url(o.sell_ex, o.asset)),
    ]
    text = (
        "🔺 <b>Связка найдена</b>\n"
        f"Buy: {o.buy_ex} — 1 {o.asset} = {o.buy_px:,.3f} $\n"
        f"Sell: {o.sell_ex} — 1 {o.asset} = {o.sell_px:,.3f} $\n"
        f"Net-профит: <b>+{o.net_pct:.2f}%</b> (после комиссий)\n"
        f"Реком. объём: 20–40 USDT\n"
        f"ETA сделки: ~{o.eta} мин"
    )
    safe_send(text, buttons=buttons)

# ====================== ALERT LOOP ======================

async def alert_loop() -> None:
    global LAST_SENT
    while True:
        try:
            for s in list(CFG.watch):
                px = mid_price(s)
                if px is None:
                    continue
                SERIES.setdefault(s, Series(CFG.rsi_period)).add(px)

                # EMA кросс
                sig = SERIES[s].cross()
                if sig:
                    send_ema_cross(s, "BULL" if sig == 'bull' else "BEAR", px)

                # «сильный» толчок
                pm = SERIES[s].pct_move(CFG.window_min)
                if pm is None:
                    continue

                # фильтр EMA200 (если включен и уже рассчитан)
                ema_ok = True
                if CFG.ema200_on:
                    if SERIES[s].ema_f is not None and SERIES[s].ema_200 is not None:
                        ema_ok = SERIES[s].ema_f >= SERIES[s].ema_200
                    else:
                        ema_ok = True  # ещё недостаточно точек — не блокируем

                # ключ антиспама
                direction = "LONG" if pm >= 0 else "SHORT"
                key = f"scalp:{s}:{direction}"
                cooldown_ok = (time.time() - LAST_SENT.get(key, 0)) > CFG.debounce_sec

                if abs(pm) >= CFG.strong_move_pct and cooldown_ok and ema_ok:
                    LAST_SENT[key] = time.time()
                    rsi_val = SERIES[s].rsi()
                    send_scalp_signal(s, direction, px, pm, rsi_val)

            # арбитраж
            if CFG.arb_on:
                base_syms = [x for x in CFG.watch if x in ("BTC","ETH","SOL","TON","BNB","XRP","ADA","TRX")]
                opps = compute_arbitrage(base_syms)
                for o in opps:
                    k = f"arb:{o.asset}:{o.buy_ex}>{o.sell_ex}"
                    if (time.time() - LAST_SENT.get(k, 0)) > max(CFG.debounce_sec // 2, 300):
                        LAST_SENT[k] = time.time()
                        send_arb(o)

        except Exception as e:
            print("alert_loop error:", e)

        await asyncio.sleep(12)  # частота опроса

# ====================== COMMANDS ======================

def parse_minutes(s: str) -> Optional[int]:
    try:
        s = s.strip().lower()
        if s.endswith("m"):
            return int(float(s[:-1]))
        if s.endswith("min"):
            return int(float(s[:-3]))
        if s.endswith("h"):
            return int(float(s[:-1]) * 60)
        return int(float(s))
    except Exception:
        return None

@bot.message_handler(commands=["start"])
def cmd_start(m):
    global CHAT_ID
    if CHAT_ID == 0:
        CHAT_ID = m.chat.id
    bot.reply_to(m,
        "Привет! Бот запущен ✅.\n"
        "Команды: /status, /price BTC, /assets, /set_assets BTC,ETH,…\n"
        "Сигналы: /signals on|off, /set_strong 2.2, /set_window 10m\n"
        "Риск: /set_sl 1.8, /set_tp 1.5 2.5 4\n"
        "Индикаторы: /ema200 on|off, /set_rsi 14\n"
        "Арбитраж: /arb_on, /arb_off, /arb_thresh 3.0"
    )

@bot.message_handler(commands=["help"])
def cmd_help(m):
    cmd_start(m)

@bot.message_handler(commands=["status"])
def cmd_status(m):
    prov = ", ".join([f"{k}:{'ON' if v.enabled else 'OFF'}" for k,v in CFG.providers.items()])
    tps = ", ".join([f"{x:.1f}%" for x in CFG.tp_pcts])
    bot.reply_to(m,
        f"Watch: {', '.join(CFG.watch)} | Profit≥{CFG.arb_min_profit_pct:.1f}%\n"
        f"window={CFG.window_min}m, strong≥{CFG.strong_move_pct:.1f}%, debounce={CFG.debounce_sec}s\n"
        f"EMA200={'ON' if CFG.ema200_on else 'OFF'}, RSI p={CFG.rsi_period} (hot {CFG.rsi_hot}/cold {CFG.rsi_cold})\n"
        f"Risk: SL {CFG.sl_pct:.1f}%, TP {tps} | Arb={'ON' if CFG.arb_on else 'OFF'}\n"
        f"Sources: {prov}"
    )

@bot.message_handler(commands=["assets"])
def cmd_assets(m):
    bot.reply_to(m, ", ".join(CFG.watch))

@bot.message_handler(commands=["set_assets"])
def cmd_set_assets(m):
    raw = m.text.split(" ", 1)
    if len(raw) < 2:
        bot.reply_to(m, "Использование: /set_assets BTC,ETH,SOL")
        return
    parts = [x.strip().upper() for x in raw[1].replace(";", ",").split(",") if x.strip()]
    if not parts:
        bot.reply_to(m, "Пустой список")
        return
    CFG.watch = parts
    for s in parts:
        SERIES.setdefault(s, Series(CFG.rsi_period))
    bot.reply_to(m, "OK. Watch обновлён.")

@bot.message_handler(commands=["price"])
def cmd_price(m):
    parts = m.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(m, "Использование: /price BTC")
        return
    sym = parts[1].upper()
    lines = [f"<b>{sym}/USDT</b>"]
    for ex in ("MEXC","KuCoin","Gate.io"):
        if not CFG.providers[ex].enabled:
            continue
        try:
            q = PROVIDER_FUN[ex](sym)
            if q:
                bid, ask = q
                mid = (bid + ask) / 2.0
                lines.append(f"{ex:<7} — {mid:,.6f} $ (mid)")
            else:
                lines.append(f"{ex:<7} — n/a")
        except Exception:
            lines.append(f"{ex:<7} — n/a")
    if sym in ("ETH","BTC") and CFG.providers["DEX"].enabled:
        px = dex_uniswap_usd(sym)
        lines.append(f"Uniswap — {px:,.6f} $" if px else "Uniswap — n/a")
    bot.reply_to(m, "\n".join(lines))

@bot.message_handler(commands=["signals"])
def cmd_signals(m):
    parts = m.text.strip().split()
    global ALERTS_ON
    if len(parts) == 2 and parts[1].lower() in ("on","off"):
        ALERTS_ON = (parts[1].lower() == "on")
    bot.reply_to(m, f"Сигналы: {'ON' if ALERTS_ON else 'OFF'}")

@bot.message_handler(commands=["set_strong"])
def cmd_set_strong(m):
    parts = m.text.strip().split()
    try:
        CFG.strong_move_pct = float(parts[1])
        bot.reply_to(m, f"OK. strong = {CFG.strong_move_pct:.2f}%")
    except Exception:
        bot.reply_to(m, "Использование: /set_strong 2.2")

@bot.message_handler(commands=["set_window"])
def cmd_set_window(m):
    parts = m.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(m, "Использование: /set_window 10m")
        return
    mins = parse_minutes(parts[1])
    if mins is None or mins <= 0:
        bot.reply_to(m, "Не понял число минут, пример: /set_window 10m")
        return
    CFG.window_min = mins
    bot.reply_to(m, f"OK. window = {CFG.window_min} мин")

@bot.message_handler(commands=["set_sl"])
def cmd_set_sl(m):
    parts = m.text.strip().split()
    try:
        CFG.sl_pct = float(parts[1])
        bot.reply_to(m, f"OK. SL = {CFG.sl_pct:.2f}%")
    except Exception:
        bot.reply_to(m, "Использование: /set_sl 1.8")

@bot.message_handler(commands=["set_tp"])
def cmd_set_tp(m):
    parts = m.text.strip().split()
    try:
        tps = [float(x) for x in parts[1:]]
        if not tps:
            raise ValueError
        CFG.tp_pcts = tps[:3]
        bot.reply_to(m, "OK. TP = " + ", ".join([f"{x:.2f}%" for x in CFG.tp_pcts]))
    except Exception:
        bot.reply_to(m, "Использование: /set_tp 1.5 2.5 4")

@bot.message_handler(commands=["ema200"])
def cmd_ema200(m):
    parts = m.text.strip().split()
    if len(parts) == 2 and parts[1].lower() in ("on","off"):
        CFG.ema200_on = (parts[1].lower() == "on")
    bot.reply_to(m, f"EMA200: {'ON' if CFG.ema200_on else 'OFF'}")

@bot.message_handler(commands=["set_rsi"])
def cmd_set_rsi(m):
    parts = m.text.strip().split()
    try:
        CFG.rsi_period = max(5, int(parts[1]))
        for s in SERIES.values():
            s.rsi_period = CFG.rsi_period
            s.rsi_prices = deque(maxlen=CFG.rsi_period + 1)
        bot.reply_to(m, f"OK. RSI period = {CFG.rsi_period}")
    except Exception:
        bot.reply_to(m, "Использование: /set_rsi 14")

@bot.message_handler(commands=["arb_on"])
def cmd_arb_on(m):
    CFG.arb_on = True
    bot.reply_to(m, "Arbitrage: ON")

@bot.message_handler(commands=["arb_off"])
def cmd_arb_off(m):
    CFG.arb_on = False
    bot.reply_to(m, "Arbitrage: OFF")

@bot.message_handler(commands=["arb_thresh"])
def cmd_arb_thr(m):
    parts = m.text.strip().split()
    try:
        CFG.arb_min_profit_pct = float(parts[1])
        bot.reply_to(m, f"OK. min profit = {CFG.arb_min_profit_pct:.2f}%")
    except Exception:
        bot.reply_to(m, "Использование: /arb_thresh 3.0")

# ====================== KEEPALIVE ======================

async def _health(request):
    return web.Response(text="OK", content_type="text/plain")

async def _web_main():
    app = web.Application()
    app.router.add_get('/health', _health)
    port = int(os.getenv('PORT', '8080'))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Keepalive /health on {port}")
    while True:
        await asyncio.sleep(3600)

def _start_web():
    def runner():
        try:
            asyncio.run(_web_main())
        except Exception as e:
            print("web error:", e)
    threading.Thread(target=runner, daemon=True).start()

# ====================== MAIN ======================

def _poll():
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20)
        except Exception as e:
            print("polling error:", e)
            time.sleep(5)

async def _main_async():
    _start_web()
    threading.Thread(target=_poll, daemon=True).start()
    await alert_loop()

if __name__ == "__main__":
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        print("Бот остановлен вручную")
