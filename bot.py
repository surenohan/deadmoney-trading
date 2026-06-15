"""
DEAD MONEY — Live Trading Bot (v4: Pyramid + Trailing + Time Exit)
Binance USDT-M Futures, runs 24/7 on Railway.

Strategy ported from paper-trading-v4.html:
- Multi-timeframe trend scan (1D/4H/1H/15m)
- Quality Score (qScore) + 3 Witnesses confirmation
- SL = 1.5 ATR, TP1 = 2.5 ATR, close 100% at TP1
- Trailing stop activates at 30% progress to TP1 (1.0 ATR trail)
- Pyramiding (+50% size) at 50% progress to TP1
- Time-based exit: close if open >8h and <30% progress
- Session filter (skip 20:00-02:00 UTC dead zone)
- Correlation filter (max 2 positions per sector)
- Volatility filter (ATR 0.6%-8% of price)
- Commission filter (skip if fees eat >50% of TP1 profit)
- Daily loss limit (3% of equity)
"""

import os
import json
import time
import math
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ═══════════════════════════════════════════════════════════
#  CONFIG (from environment variables)
# ═══════════════════════════════════════════════════════════
BINANCE_API_KEY    = os.environ["BINANCE_API_KEY"]
BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# Strategy params (override via env vars if desired)
MIN_SCORE      = int(os.environ.get("MIN_SCORE", 70))
MIN_WITNESSES  = int(os.environ.get("MIN_WITNESSES", 2))
MAX_POSITIONS  = int(os.environ.get("MAX_POSITIONS", 5))
SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL_MIN", 30)) * 60  # seconds
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PCT", 10))
DIRECTION      = os.environ.get("DIRECTION", "both")  # both | long | short
LEVERAGE       = int(os.environ.get("LEVERAGE", 20))
RISK_USD       = float(os.environ.get("RISK_USD", 0.5))  # fixed USDT risk per trade (independent of account balance)
TEST_MODE      = os.environ.get("TEST_MODE", "false").lower() == "true"  # if true, open only ONE position total then stop scanning

STATE_FILE = Path("/data/state.json") if Path("/data").exists() else Path("state.json")

# ═══════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("deadmoney")

# ═══════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════
def tg_send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def tg_get_updates(offset=None):
    try:
        params = {"timeout": 5}
        if offset:
            params["offset"] = offset
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params=params, timeout=10,
        )
        return r.json().get("result", [])
    except Exception as e:
        log.warning(f"Telegram getUpdates failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "positions": {},   # symbol -> position dict
        "trades": [],
        "last_signals": {},
        "day_start_equity": None,
        "day_start_date": None,
        "auto_on": True,
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ═══════════════════════════════════════════════════════════
#  BINANCE CLIENT
# ═══════════════════════════════════════════════════════════
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# ─── Algo Order endpoints (TP/SL conditional orders) ────────
# As of 2025-12-09, Binance moved STOP_MARKET/TAKE_PROFIT_MARKET to a
# dedicated Algo Order API not yet supported by python-binance==1.0.19.
# We call these endpoints directly with the same signing as the client.
import hmac
import hashlib
from urllib.parse import urlencode

FAPI_BASE = "https://fapi.binance.com"


def _signed_request(method, path, params):
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    query = urlencode(params, True)
    sig = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    query += f"&signature={sig}"
    url = f"{FAPI_BASE}{path}?{query}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    r = requests.request(method, url, headers=headers, timeout=15)
    data = r.json()
    if r.status_code >= 400:
        raise BinanceAPIException(r, r.status_code, r.text)
    return data


def algo_create_stop(symbol, side, order_type, stop_price):
    """Place STOP_MARKET or TAKE_PROFIT_MARKET via the new Algo Order API."""
    return _signed_request("POST", "/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "triggerPrice": stop_price,
        "closePosition": "true",
        "workingType": "MARK_PRICE",
    })


def algo_cancel_all(symbol):
    """Cancel all open algo (conditional) orders for a symbol."""
    try:
        return _signed_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})
    except BinanceAPIException as e:
        log.warning(f"algo_cancel_all {symbol}: {e}")
        return None


def get_equity():
    """Total USDT futures wallet balance."""
    bal = client.futures_account_balance()
    for b in bal:
        if b["asset"] == "USDT":
            return float(b["balance"])
    return 0.0


def get_klines(symbol, interval, limit=80):
    try:
        raw = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        return [
            {"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
             "c": float(k[4]), "v": float(k[5])}
            for k in raw
        ]
    except BinanceAPIException as e:
        log.warning(f"klines {symbol} {interval}: {e}")
        return None


def get_price(symbol):
    try:
        return float(client.futures_symbol_ticker(symbol=symbol)["price"])
    except Exception:
        return None


def get_24h_change(symbol):
    try:
        return float(client.futures_ticker(symbol=symbol)["priceChangePercent"])
    except Exception:
        return 0.0


def get_usdt_perpetuals():
    info = client.futures_exchange_info()
    syms = []
    for s in info["symbols"]:
        if (s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
                and s["contractType"] == "PERPETUAL" and "1000000" not in s["symbol"]):
            syms.append(s)
    return syms


def symbol_filters(sym_info):
    """Return (qty_step, price_step, min_notional)."""
    qty_step = price_step = 0.0
    min_notional = 5.0
    for f in sym_info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            qty_step = float(f["stepSize"])
        elif f["filterType"] == "PRICE_FILTER":
            price_step = float(f["tickSize"])
        elif f["filterType"] == "MIN_NOTIONAL":
            min_notional = float(f.get("notional", f.get("minNotional", 5)))
    return qty_step, price_step, min_notional


def round_step(value, step):
    if step == 0:
        return value
    precision = max(0, int(round(-math.log10(step))))
    return round(math.floor(value / step) * step, precision)


# ═══════════════════════════════════════════════════════════
#  MATH / ANALYSIS (ported from JS)
# ═══════════════════════════════════════════════════════════
def ema(data, period):
    k = 2 / (period + 1)
    result = [data[0]]
    for v in data[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains = losses = 0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100
    return 100 - 100 / (1 + avg_g / avg_l)


def avg_vol(candles, period=20):
    s = candles[-period:]
    return sum(c["v"] for c in s) / len(s)


def calc_atr(candles, period=14):
    if len(candles) < period + 2:
        c = candles[-1]
        return c["h"] - c["l"]
    total = 0
    for i in range(len(candles) - period, len(candles)):
        prev = candles[i - 1] if i > 0 else candles[i]
        total += max(
            candles[i]["h"] - candles[i]["l"],
            abs(candles[i]["h"] - prev["c"]),
            abs(candles[i]["l"] - prev["c"]),
        )
    return total / period


def aTF(candles):
    """Trend analysis for one timeframe."""
    if not candles or len(candles) < 30:
        return {"trend": 0, "score": 0, "rsiV": 50, "vS": False}
    closes = [c["c"] for c in candles]
    n = len(closes) - 1
    e20 = ema(closes, 20)
    e50 = ema(closes, min(50, len(closes) - 1))
    rv = round(rsi(closes, 14))
    vs = candles[n]["v"] > avg_vol(candles, 20) * 1.3

    h10 = [c["h"] for c in candles[-10:]]
    l10 = [c["l"] for c in candles[-10:]]
    hh = h10[9] > h10[4] > h10[0]
    ll = l10[9] < l10[4] < l10[0]
    tU = closes[n] > e20[n] > e50[n]
    tD = closes[n] < e20[n] < e50[n]

    trend, score = 0, 0
    if tU and hh:
        trend, score = 1, 2
    elif tD and ll:
        trend, score = -1, 2
    elif tU or hh:
        trend, score = 1, 1
    elif tD or ll:
        trend, score = -1, 1

    if vs and trend != 0:
        score = min(score + 0.5, 2)
    if rv < 32 and trend >= 0:
        score += 0.5
        trend = max(trend, 1)
    if rv > 68 and trend <= 0:
        score += 0.5
        trend = min(trend, -1)

    return {"trend": trend, "score": score, "rsiV": rv, "vS": vs}


def qScore(t1d, t4h, t1h, t15, direction):
    v1d = 1 if t1d["trend"] == direction else 0
    v4h = 1 if t4h["trend"] == direction else 0
    v1h = 1 if t1h["trend"] == direction else 0
    v15 = 1 if t15["trend"] == direction else 0
    if not v4h:
        return 0
    if not v1d and not v1h:
        return 0
    b = 30 + v1d * 24 + v4h * 22 + v1h * 16 + v15 * 8
    b += t4h["score"] * 3 + t1h["score"] * 2
    if t4h["rsiV"] < 35:
        b += 7
    if t4h["rsiV"] > 65:
        b -= 5
    if t1h["rsiV"] < 38:
        b += 4
    if t1h["rsiV"] > 62:
        b -= 3
    return min(max(round(b), 10), 95)


def calcWits(k4h, k1h, k15, direction):
    if not k4h or not k1h or not k15:
        return {"w1": False, "w2": False, "w3": False, "n": 0}
    tf4h = aTF(k4h)
    w1 = tf4h["trend"] == direction and tf4h["score"] >= 1
    c1h = k1h[-2] if len(k1h) >= 2 else k1h[-1]
    body1h = abs(c1h["c"] - c1h["o"])
    rng1h = c1h["h"] - c1h["l"]
    w2 = rng1h > 0 and body1h / rng1h >= 0.50 and (1 if c1h["c"] > c1h["o"] else -1) == direction
    w3 = k4h[-1]["v"] > avg_vol(k4h, 20) * 1.15
    n = sum([w1, w2, w3])
    return {"w1": w1, "w2": w2, "w3": w3, "n": n}


# Sector map for correlation filter
SECTORS = {
    "BTC": ["BTC"], "ETH": ["ETH"],
    "LAYER1": ["SOL","AVAX","ADA","DOT","ATOM","NEAR","APT","SUI","SEI","TIA","INJ","TON","ALGO"],
    "LAYER2": ["ARB","OP","MATIC","STRK"],
    "DeFi": ["UNI","AAVE","CRV","DYDX","GMX","SNX"],
    "MEME": ["DOGE","SHIB","FLOKI","PEPE","WIF","BONK"],
    "AI": ["FET","AGIX","OCEAN","RNDR","WLD","TAO"],
    "EXCHANGE": ["BNB","OKB","CRO"],
    "GAMING": ["AXS","SAND","MANA","GALA"],
    "INFRA": ["LINK","GRT","TRX","XRP","LTC","BCH","XLM"],
}


def get_sector(base):
    for sec, syms in SECTORS.items():
        if base in syms:
            return sec
    return "OTHER"


# ═══════════════════════════════════════════════════════════
#  TRADING LOGIC
# ═══════════════════════════════════════════════════════════
def is_dead_zone():
    h = datetime.now(timezone.utc).hour
    return h >= 20 or h < 2


def check_daily_limit(state, equity):
    today = datetime.now(timezone.utc).date().isoformat()
    if state["day_start_date"] != today:
        state["day_start_date"] = today
        state["day_start_equity"] = equity
        save_state(state)
    start_eq = state["day_start_equity"] or equity
    loss_pct = (equity - start_eq) / start_eq * 100
    return loss_pct <= -MAX_DAILY_LOSS_PCT


def get_max_leverage(symbol):
    """Return the maximum leverage allowed for this symbol (first/lowest bracket)."""
    try:
        brackets = client.futures_leverage_bracket(symbol=symbol)
        if brackets and brackets[0].get("brackets"):
            return int(brackets[0]["brackets"][0]["initialLeverage"])
    except BinanceAPIException as e:
        log.warning(f"leverage_bracket {symbol}: {e}")
    return LEVERAGE  # fallback: assume desired leverage is fine


def set_leverage(symbol, leverage):
    try:
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
    except BinanceAPIException as e:
        log.warning(f"set_leverage {symbol}: {e}")


def open_position(state, symbol, base, direction, price, sl, tp1, atrV, score, witnesses, equity):
    """Place market order + SL + TP on Binance Futures."""
    info = next((s for s in get_usdt_perpetuals() if s["symbol"] == symbol), None)
    if not info:
        log.warning(f"{symbol}: not found in exchange info")
        return False
    qty_step, price_step, min_notional = symbol_filters(info)

    # Use the lesser of desired leverage and what this symbol allows
    max_lev = get_max_leverage(symbol)
    eff_leverage = min(LEVERAGE, max_lev)

    risk_usdt = RISK_USD
    sl_distance = abs(price - sl)
    if sl_distance == 0:
        return False

    # Position size such that risk_usdt = qty * sl_distance
    qty = risk_usdt / sl_distance
    notional = qty * price

    # Margin required at effective leverage
    margin_required = notional / eff_leverage

    # Ensure min notional met (scale up if needed, cap risk increase at 2x)
    if notional < min_notional:
        scale = min_notional / notional
        if scale > 2.0:
            log.info(f"⊘ SKIP {base}: notional ${notional:.2f} too small even after 2x scale (need ${min_notional}, max lev {max_lev}x)")
            return False
        qty *= scale
        notional *= scale
        margin_required = notional / eff_leverage

    # Safety: never risk more than 25% of equity as margin on one trade
    if margin_required > equity * 0.25:
        log.info(f"⊘ SKIP {base}: margin ${margin_required:.2f} would exceed 25% of equity (lev {eff_leverage}x, min_notional ${min_notional})")
        return False

    qty = round_step(qty, qty_step)
    if qty <= 0:
        log.info(f"⊘ SKIP {base}: qty rounds to 0")
        return False

    sl_price = round_step(sl, price_step)
    tp_price = round_step(tp1, price_step)

    set_leverage(symbol, eff_leverage)

    side = "BUY" if direction == 1 else "SELL"
    opp_side = "SELL" if direction == 1 else "BUY"

    try:
        order = client.futures_create_order(
            symbol=symbol, side=side, type="MARKET", quantity=qty,
        )
        entry_price = price  # approx; could fetch fill price from order
        try:
            entry_price = float(order.get("avgPrice", 0)) or price
        except Exception:
            pass

    except BinanceAPIException as e:
        log.error(f"open_position {symbol}: {e}")
        tg_send(f"❌ Error opening {base}: {e}")
        return False

    # ── Place SL/TP via Algo Order API ────────────────────────
    # CRITICAL: if either fails, the position is unprotected.
    # In that case, immediately close it at market — never leave
    # a live position without a stop-loss.
    try:
        algo_create_stop(symbol, opp_side, "STOP_MARKET", sl_price)
        algo_create_stop(symbol, opp_side, "TAKE_PROFIT_MARKET", tp_price)
    except BinanceAPIException as e:
        log.error(f"SL/TP placement failed for {base}: {e} — closing position immediately")
        try:
            client.futures_create_order(
                symbol=symbol, side=opp_side, type="MARKET",
                quantity=qty, reduceOnly=True,
            )
        except BinanceAPIException as e2:
            log.error(f"EMERGENCY CLOSE FAILED for {base}: {e2}")
            tg_send(f"🆘 <b>{base}</b>: SL/TP failed AND emergency close failed! Check manually NOW: {e2}")
            return False
        tg_send(f"⚠️ <b>{base}</b>: SL/TP order failed ({e}), position closed immediately for safety.")
        return False

    pos = {
        "symbol": symbol, "base": base, "dir": direction,
        "entry": entry_price, "stop": sl_price, "stop0": sl_price,
        "tp1": tp_price, "qty": qty, "atrV": atrV,
        "score": score, "witnesses": witnesses, "leverage": eff_leverage,
        "risk": risk_usdt, "tp1_hit": False,
        "trail_active": False, "pyramid_done": False,
        "opened_at": time.time(),
    }
    state["positions"][symbol] = pos
    save_state(state)

    dir_label = "LONG ▲" if direction == 1 else "SHORT ▼"
    tg_send(
        f"⚡ <b>OPEN {dir_label} {base}</b>\n"
        f"Entry: {entry_price}\nSL: {sl_price}  TP1: {tp_price}\n"
        f"Qty: {qty}  Notional: ${notional:.2f}  Margin: ${margin_required:.2f}\n"
        f"Leverage: {eff_leverage}x{' (capped, max for symbol)' if eff_leverage < LEVERAGE else ''}\n"
        f"Score: {score}  Witnesses: {witnesses}/3"
    )
    log.info(f"OPEN {dir_label} {base} @ {entry_price} sl={sl_price} tp={tp_price} qty={qty}")
    return True


def close_position(state, symbol, reason, price=None):
    pos = state["positions"].get(symbol)
    if not pos:
        return
    side = "SELL" if pos["dir"] == 1 else "BUY"
    try:
        # Cancel remaining algo orders (SL/TP) and any regular open orders
        algo_cancel_all(symbol)
        try:
            client.futures_cancel_all_open_orders(symbol=symbol)
        except Exception:
            pass
        # Close any remaining position at market
        positions = client.futures_position_information(symbol=symbol)
        amt = float(positions[0]["positionAmt"]) if positions else 0
        if amt != 0:
            client.futures_create_order(
                symbol=symbol, side=side, type="MARKET",
                quantity=abs(amt), reduceOnly=True,
            )
    except BinanceAPIException as e:
        log.warning(f"close_position {symbol}: {e}")

    cur_price = price or get_price(symbol)
    pnl_est = (cur_price - pos["entry"]) * pos["dir"] * pos["qty"]
    if pos.get("pyramid"):
        pyr = pos["pyramid"]
        pnl_est += (cur_price - pyr["entry"]) * pos["dir"] * pyr["qty"]

    trade = {
        "symbol": symbol, "base": pos["base"], "dir": pos["dir"],
        "entry": pos["entry"], "exit": cur_price, "reason": reason,
        "pnl_est": pnl_est, "score": pos["score"],
        "opened_at": pos["opened_at"], "closed_at": time.time(),
    }
    state["trades"].insert(0, trade)
    del state["positions"][symbol]
    save_state(state)

    emoji = "✅" if pnl_est >= 0 else "🔴"
    tg_send(
        f"{emoji} <b>CLOSE {pos['base']} — {reason}</b>\n"
        f"Entry: {pos['entry']} → Exit: {cur_price}\n"
        f"Est. P&L: {pnl_est:+.2f} USDT"
    )
    log.info(f"CLOSE {pos['base']} {reason} entry={pos['entry']} exit={cur_price} pnl~{pnl_est:.2f}")


def add_pyramid(state, symbol, price, atrVal, equity):
    pos = state["positions"][symbol]
    d = pos["dir"]
    risk_usdt = pos["risk"] * 0.5
    sl_distance = atrVal * 0.8
    add_qty = risk_usdt / sl_distance if sl_distance > 0 else 0

    info = next((s for s in get_usdt_perpetuals() if s["symbol"] == symbol), None)
    qty_step, price_step, min_notional = symbol_filters(info) if info else (0, 0, 5)
    add_qty = round_step(add_qty, qty_step)
    if add_qty <= 0:
        return

    notional = add_qty * price
    if notional < min_notional:
        return  # too small to pyramid, skip silently

    side = "BUY" if d == 1 else "SELL"
    try:
        client.futures_create_order(symbol=symbol, side=side, type="MARKET", quantity=add_qty)
    except BinanceAPIException as e:
        log.warning(f"pyramid {symbol}: {e}")
        return

    new_stop = round_step(price - atrVal * 0.8 * d, price_step)
    pos["pyramid"] = {"entry": price, "qty": add_qty, "stop": new_stop}
    pos["pyramid_done"] = True
    pos["risk"] += risk_usdt
    save_state(state)

    tg_send(f"🔺 <b>RAISE {pos['base']}</b> — pyramid +50% @ {price}")
    log.info(f"PYRAMID {pos['base']} @ {price} qty+{add_qty}")


def update_trailing_stop(state, symbol, price):
    """Move SL order to trail price; returns True if stop was updated."""
    pos = state["positions"][symbol]
    d = pos["dir"]
    atrVal = pos["atrV"]

    dist_to_tp1 = abs(pos["tp1"] - pos["entry"])
    dist_moved = (price - pos["entry"]) * d
    progress = dist_moved / dist_to_tp1 if dist_to_tp1 else 0

    if progress >= 0.3 and not pos["trail_active"]:
        pos["trail_active"] = True
        tg_send(f"⟳ TRAIL activated for {pos['base']} (30% to TP1)")

    if progress >= 0.5 and not pos["pyramid_done"] and not pos["tp1_hit"]:
        equity = get_equity()
        add_pyramid(state, symbol, price, atrVal, equity)

    if pos["trail_active"]:
        new_stop = price - atrVal * 1.0 * d
        should_update = (new_stop > pos["stop"]) if d == 1 else (new_stop < pos["stop"])
        if should_update:
            info = next((s for s in get_usdt_perpetuals() if s["symbol"] == symbol), None)
            _, price_step, _ = symbol_filters(info) if info else (0, 0, 5)
            new_stop_r = round_step(new_stop, price_step)
            try:
                algo_cancel_all(symbol)
                opp_side = "SELL" if d == 1 else "BUY"
                algo_create_stop(symbol, opp_side, "STOP_MARKET", new_stop_r)
                algo_create_stop(symbol, opp_side, "TAKE_PROFIT_MARKET", pos["tp1"])
                pos["stop"] = new_stop_r
                save_state(state)
            except BinanceAPIException as e:
                log.warning(f"trail update {symbol}: {e}")

    # Time-based exit
    hours_open = (time.time() - pos["opened_at"]) / 3600
    if hours_open > 8 and progress < 0.30 and not pos["tp1_hit"]:
        close_position(state, symbol, "TIME-EXIT", price)
        return

    # Manual TP1/SL check (in case Binance order didn't fire yet, e.g. due to trail race)
    if (d == 1 and price >= pos["tp1"]) or (d == -1 and price <= pos["tp1"]):
        close_position(state, symbol, "TP1", pos["tp1"])
    elif (d == 1 and price <= pos["stop"]) or (d == -1 and price >= pos["stop"]):
        close_position(state, symbol, "TRAIL-STOP" if pos["trail_active"] else "STOP", pos["stop"])


def monitor_positions(state):
    for symbol in list(state["positions"].keys()):
        price = get_price(symbol)
        if price is None:
            continue
        # Check if Binance already closed it (SL/TP order filled)
        try:
            pos_info = client.futures_position_information(symbol=symbol)
            amt = float(pos_info[0]["positionAmt"]) if pos_info else 0
        except Exception:
            amt = None

        if amt == 0:
            # Position closed externally (SL or TP hit)
            pos = state["positions"][symbol]
            pnl_est = (price - pos["entry"]) * pos["dir"] * pos["qty"]
            reason = "TP1" if pnl_est >= 0 else "STOP"
            close_position(state, symbol, reason, price)
            continue

        update_trailing_stop(state, symbol, price)


# ═══════════════════════════════════════════════════════════
#  SCANNER
# ═══════════════════════════════════════════════════════════
def scan_and_trade(state):
    equity = get_equity()

    if check_daily_limit(state, equity):
        log.info("🛑 Daily loss limit reached — scan skipped")
        tg_send("🛑 Daily loss limit reached. Scan paused until tomorrow (UTC).")
        return

    if is_dead_zone():
        log.info("🌑 Dead zone (20-02 UTC) — scan skipped")
        return

    if len(state["positions"]) >= MAX_POSITIONS:
        log.info(f"Max positions ({MAX_POSITIONS}) reached — skip scan")
        return

    if TEST_MODE and state.get("trades") or (TEST_MODE and state["positions"]):
        # TEST_MODE: stop after the very first trade (open or closed)
        if state["positions"] or state["trades"]:
            log.info("TEST_MODE: one trade already opened/closed — scan paused. Set TEST_MODE=false to resume normal operation.")
            return

    symbols = get_usdt_perpetuals()
    found = []
    prev_sigs = state.get("last_signals", {})
    new_sigs = {}

    for info in symbols:
        symbol = info["symbol"]
        base = symbol.replace("USDT", "")

        if symbol in state["positions"]:
            continue

        # Correlation filter
        sym_sec = get_sector(base)
        sec_count = sum(1 for p in state["positions"].values() if get_sector(p["base"]) == sym_sec)
        if sec_count >= 2:
            continue

        k1d = get_klines(symbol, "1d", 80)
        k4h = get_klines(symbol, "4h", 80)
        k1h = get_klines(symbol, "1h", 60)
        k15m = get_klines(symbol, "15m", 60)
        if not all([k1d, k4h, k1h, k15m]):
            continue

        price = k15m[-1]["c"]
        atrV = calc_atr(k15m, 14)
        chg = get_24h_change(symbol)

        if abs(chg) > 18:
            continue
        if (k15m[-1]["h"] - k15m[-1]["l"]) / atrV > 3.5:
            continue
        atr_pct = atrV / price * 100
        if atr_pct < 0.6 or atr_pct > 8:
            continue

        t1d, t4h, t1h, t15 = aTF(k1d), aTF(k4h), aTF(k1h), aTF(k15m)
        longs = sum(1 for t in (t1d, t4h, t1h, t15) if t["trend"] == 1)
        shorts = sum(1 for t in (t1d, t4h, t1h, t15) if t["trend"] == -1)

        direction = 0
        if longs >= 3:
            direction = 1
        elif shorts >= 3:
            direction = -1
        elif longs == 2 and t15["trend"] == 1 and t1h["trend"] == 1:
            direction = 1
        elif shorts == 2 and t15["trend"] == -1 and t1h["trend"] == -1:
            direction = -1
        if not direction:
            continue
        if DIRECTION == "long" and direction != 1:
            continue
        if DIRECTION == "short" and direction != -1:
            continue

        sc = qScore(t1d, t4h, t1h, t15, direction)
        if sc < MIN_SCORE:
            continue
        wt = calcWits(k4h, k1h, k15m, direction)
        if wt["n"] < MIN_WITNESSES:
            continue

        # Levels (v4: SL=1.5 ATR, TP1=2.5 ATR)
        sl = price - atrV * 1.5 * direction
        tp1 = price + atrV * 2.5 * direction

        # Commission filter
        risk_usdt = RISK_USD
        sl_dist = abs(price - sl)
        qty_est = risk_usdt / sl_dist if sl_dist else 0
        notional = qty_est * price
        comm_round_trip = notional * 0.001  # 0.05% x2
        tp1_profit = risk_usdt * 2.5
        if (tp1_profit - comm_round_trip) < comm_round_trip * 2:
            continue

        # Signal persistence
        sig_key = f"{base}_{direction}"
        prev = prev_sigs.get(sig_key)
        new_sigs[sig_key] = {"dir": direction, "sc": sc, "tf4h": t4h["trend"], "ts": time.time()}
        persisted = prev and prev.get("tf4h") == t4h["trend"] and (time.time() - prev.get("ts", 0)) < 4 * 3600
        final_sc = min(sc + 5, 95) if persisted else sc

        found.append({
            "symbol": symbol, "base": base, "dir": direction, "sc": final_sc,
            "wn": wt["n"], "price": price, "sl": sl, "tp1": tp1, "atrV": atrV,
        })

    state["last_signals"] = {**prev_sigs, **new_sigs}
    save_state(state)

    found.sort(key=lambda x: -x["sc"])
    log.info(f"Scan complete — {len(found)} signals")

    slots = MAX_POSITIONS - len(state["positions"])
    if TEST_MODE:
        slots = min(slots, 1)
    for sig in found[:slots]:
        open_position(
            state, sig["symbol"], sig["base"], sig["dir"], sig["price"],
            sig["sl"], sig["tp1"], sig["atrV"], sig["sc"], sig["wn"], equity,
        )
        time.sleep(0.5)


# ═══════════════════════════════════════════════════════════
#  TELEGRAM COMMAND HANDLER (runs in background thread)
# ═══════════════════════════════════════════════════════════
def telegram_command_loop(state):
    offset = None
    while True:
        try:
            updates = tg_get_updates(offset)
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message", {}).get("text", "")
                if not msg:
                    continue
                handle_command(state, msg.strip().lower())
        except Exception as e:
            log.warning(f"telegram loop error: {e}")
        time.sleep(3)


def handle_command(state, cmd):
    if cmd == "/status":
        equity = get_equity()
        n_pos = len(state["positions"])
        lines = [f"💰 Equity: ${equity:.2f}", f"📂 Open positions: {n_pos}"]
        for sym, p in state["positions"].items():
            dirl = "LONG" if p["dir"] == 1 else "SHORT"
            lines.append(f"  {p['base']} {dirl} @ {p['entry']} (SL {p['stop']}, TP {p['tp1']})")
        tg_send("\n".join(lines))

    elif cmd == "/stats":
        trades = state["trades"]
        if not trades:
            tg_send("No closed trades yet.")
            return
        wins = [t for t in trades if t["pnl_est"] > 0]
        wr = round(len(wins) / len(trades) * 100)
        total = sum(t["pnl_est"] for t in trades)
        tg_send(f"📊 Trades: {len(trades)}\nWin rate: {wr}%\nTotal P&L (est): {total:+.2f} USDT")

    elif cmd == "/pause":
        state["auto_on"] = False
        save_state(state)
        tg_send("⏸ Auto-trading paused. Existing positions still monitored.")

    elif cmd == "/resume":
        state["auto_on"] = True
        save_state(state)
        tg_send("▶️ Auto-trading resumed.")

    elif cmd == "/closeall":
        for sym in list(state["positions"].keys()):
            close_position(state, sym, "MANUAL-CLOSE")
        tg_send("✗ All positions closed.")

    elif cmd == "/help":
        tg_send(
            "Commands:\n"
            "/status — equity & open positions\n"
            "/stats — trade statistics\n"
            "/pause — stop opening new trades\n"
            "/resume — resume auto-trading\n"
            "/closeall — close all positions now"
        )


# ═══════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════
def main():
    state = load_state()
    log.info("DEAD MONEY bot starting...")
    tg_send(
        "🤖 <b>DEAD MONEY bot started</b>\n"
        f"Equity: ${get_equity():.2f}\n"
        f"Leverage: {LEVERAGE}x | Risk: ${RISK_USD:.2f} fixed/trade\n"
        f"Min Score: {MIN_SCORE} | Min Witnesses: {MIN_WITNESSES}\n"
        f"Max positions: {MAX_POSITIONS} | Scan every {SCAN_INTERVAL//60}min\n"
        + ("⚠️ <b>TEST_MODE ON</b> — will open exactly ONE trade then pause.\n" if TEST_MODE else "")
        + "Send /help for commands."
    )

    # Background telegram command listener
    t = threading.Thread(target=telegram_command_loop, args=(state,), daemon=True)
    t.start()

    last_scan = 0
    while True:
        try:
            # Monitor open positions every 60s
            if state["positions"]:
                monitor_positions(state)

            # Run scan on interval
            if time.time() - last_scan >= SCAN_INTERVAL:
                if state.get("auto_on", True):
                    scan_and_trade(state)
                else:
                    log.info("Auto-trade paused (/pause) — skipping scan")
                last_scan = time.time()

        except Exception as e:
            log.exception(f"main loop error: {e}")
            tg_send(f"⚠️ Bot error: {e}")

        time.sleep(60)


if __name__ == "__main__":
    main()
