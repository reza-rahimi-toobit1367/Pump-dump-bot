#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات هشدار زودهنگام پامپ/دامپ - بازار فیوچرز توبیت (Toobit)
"""

import os
import time
import requests
from collections import deque
from datetime import datetime

# ============================== CONFIG ==============================

BASE_URL = "https://api.toobit.com"

TICKER_ENDPOINT = "/quote/v1/contract/ticker/24hr"
FUNDING_ENDPOINT = "/api/v1/futures/fundingRate"
DEPTH_ENDPOINT = "/quote/v1/depth"
OPEN_INTEREST_ENDPOINT = "/quote/v1/openInterest"
KLINES_ENDPOINT = "/quote/v1/klines"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_TO_TELEGRAM = True

POLL_INTERVAL_SECONDS = 20
WINDOW_SIZE = 12

EARLY_VOLUME_RATIO = 2.5
PRICE_MOVE_CAP_PCT = 2.0
MIN_QUOTE_VOLUME = 150000
MAX_CANDIDATES_PER_CYCLE = 15
MIN_VOLUME_TREND_CONSISTENCY = 0.6

ORDERBOOK_DEPTH_LEVELS = 20
ORDERBOOK_IMBALANCE_THRESHOLD = 2.2
OI_CHANGE_RATIO_THRESHOLD = 1.20
MIN_FUNDING_JUMP = 0.0015

RSI_INTERVAL = "15m"
RSI_PERIOD = 14
RSI_LONG_MAX = 68
RSI_SHORT_MIN = 32

EARLY_WARNING_MIN_SCORE = 4

CONFIRMED_PRICE_CHANGE_PCT = 6.0
CONFIRMED_VOLUME_RATIO = 3.0

ALERT_COOLDOWN_SECONDS = 20 * 60

STOP_LOSS_PCT = 10.0
TAKE_PROFIT_PCT = 10.0

MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "0"))

# ======================================================================


class SymbolState:
    def __init__(self, window_size):
        self.prices = deque(maxlen=window_size)
        self.quote_volumes = deque(maxlen=window_size)
        self.funding_rates = deque(maxlen=window_size)
        self.open_interest_history = deque(maxlen=5)
        self.last_early_alert = 0
        self.last_confirmed_alert = 0

    def is_ready(self):
        return len(self.prices) == self.prices.maxlen

    def price_change_pct(self):
        if len(self.prices) < 2 or self.prices[0] == 0:
            return 0.0
        return (self.prices[-1] - self.prices[0]) / self.prices[0] * 100

    def volume_accel_ratio(self):
        if len(self.quote_volumes) < 3:
            return 0.0
        deltas = [
            max(self.quote_volumes[i] - self.quote_volumes[i - 1], 0)
            for i in range(1, len(self.quote_volumes))
        ]
        last_delta = deltas[-1]
        prior = deltas[:-1]
        avg_prior = sum(prior) / len(prior) if prior else 0
        if avg_prior <= 0:
            return 0.0
        return last_delta / avg_prior

    def volume_trend_consistency(self):
        if len(self.quote_volumes) < 3:
            return 0.0
        deltas = [
            self.quote_volumes[i] - self.quote_volumes[i - 1]
            for i in range(1, len(self.quote_volumes))
        ]
        positive = sum(1 for d in deltas if d > 0)
        return positive / len(deltas)

    def funding_rate_jump(self):
        if len(self.funding_rates) < 2:
            return 0.0
        return abs(self.funding_rates[-1] - self.funding_rates[-2])


def http_get(path, params=None):
    url = BASE_URL + path
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def send_telegram_message(text):
    if not SEND_TO_TELEGRAM:
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("توکن/چت‌آیدی تلگرام ست نشده؛ پیام ارسال نشد.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"ارسال تلگرام ناموفق: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"خطای ارسال تلگرام: {e}")


def fetch_bulk_tickers():
    return http_get(TICKER_ENDPOINT)


def fetch_bulk_funding_rates():
    return http_get(FUNDING_ENDPOINT)


def fetch_orderbook_imbalance(symbol):
    try:
        data = http_get(DEPTH_ENDPOINT, {"symbol": symbol, "limit": ORDERBOOK_DEPTH_LEVELS})
        bid_qty = sum(float(level[1]) for level in data.get("b", []))
        ask_qty = sum(float(level[1]) for level in data.get("a", []))
        if ask_qty <= 0:
            return None
        return bid_qty / ask_qty
    except Exception:
        return None


def fetch_open_interest(symbol):
    try:
        data = http_get(OPEN_INTEREST_ENDPOINT, {"symbol": symbol})
        items = data.get("openInterestList", [])
        if items:
            return float(items[0].get("size", 0))
    except Exception:
        pass
    return None


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def fetch_rsi(symbol):
    try:
        data = http_get(
            KLINES_ENDPOINT,
            {"symbol": symbol, "interval": RSI_INTERVAL, "limit": RSI_PERIOD + 50},
        )
        closes = [float(candle[4]) for candle in data]
        if len(closes) < RSI_PERIOD + 1:
            return None
        return calculate_rsi(closes, RSI_PERIOD)
    except Exception:
        return None


def display_symbol(symbol):
    return symbol.replace("-SWAP-", "").replace("-SWAP", "")


def calc_trade_levels(entry_price, is_long):
    if is_long:
        stop = entry_price * (1 - STOP_LOSS_PCT / 100)
        target = entry_price * (1 + TAKE_PROFIT_PCT / 100)
    else:
        stop = entry_price * (1 + STOP_LOSS_PCT / 100)
        target = entry_price * (1 - TAKE_PROFIT_PCT / 100)
    return stop, target


def format_price(p):
    if p >= 100:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:,.4f}"
    else:
        return f"{p:.6f}"


def format_early_warning(symbol, price, price_change, vol_ratio, ob_imbalance, oi_ratio, funding_jump, rsi):
    now = datetime.now().strftime("%H:%M:%S")
    is_long = (ob_imbalance or 1) >= 1
    direction_hint = "خرید (احتمال پامپ)" if is_long else "فروش (احتمال دامپ)"
    position_word = "🟢 لانگ (Long)" if is_long else "🔴 شورت (Short)"
    stop, target = calc_trade_levels(price, is_long)

    lines = [
        f"🪙 *{display_symbol(symbol)}*",
        f"⚠️ هشدار زودهنگام | {now}",
        f"",
        f"این یه سیگنال احتمالی «قبل از حرکت شدید» است، نه تضمینی و نه توصیه‌ی مالی.",
        f"تغییر قیمت تا الان: {price_change:+.2f}% (هنوز کم)",
        f"جهش حجم معاملات: {vol_ratio:.1f}x میانگین",
    ]
    if ob_imbalance is not None:
        lines.append(f"عدم تعادل Order Book: {ob_imbalance:.2f} → فشار سمت {direction_hint}")
    if oi_ratio is not None:
        lines.append(f"تغییر Open Interest: {oi_ratio:.2f}x")
    if funding_jump:
        lines.append(f"جهش نرخ فاندینگ: {funding_jump:.5f}")
    if rsi is not None:
        lines.append(f"RSI ({RSI_INTERVAL}): {rsi:.1f}")

    lines += [
        f"",
        f"پیشنهاد جهت: {position_word}",
        f"نقطه ورود: {format_price(price)}",
        f"تارگت: {format_price(target)}",
        f"حد ضرر: {format_price(stop)}",
    ]
    return "\n".join(lines)


def format_confirmed_alert(symbol, direction, price_change, vol_ratio, price, rsi=None):
    now = datetime.now().strftime("%H:%M:%S")
    is_long = direction == "pump"
    arrow = "🚀 پامپ در حال وقوع" if is_long else "🔻 دامپ در حال وقوع"
    position_word = "🟢 لانگ (Long)" if is_long else "🔴 شورت (Short)"
    stop, target = calc_trade_levels(price, is_long)
    rsi_line = f"RSI ({RSI_INTERVAL}): {rsi:.1f}\n" if rsi is not None else ""
    return (
        f"🪙 *{display_symbol(symbol)}*\n"
        f"{arrow} | {now}\n\n"
        f"تغییر قیمت: {price_change:+.2f}%\n"
        f"نسبت جهش حجم: {vol_ratio:.1f}x میانگین\n"
        f"{rsi_line}"
        f"\n"
        f"پیشنهاد جهت: {position_word}\n"
        f"نقطه ورود: {format_price(price)}\n"
        f"تارگت: {format_price(target)}\n"
        f"حد ضرر: {format_price(stop)}"
    )


def main():
    print("شروع رصد فیوچرز توبیت برای نشانه‌های زودهنگام پامپ/دامپ ...")
    print(f"مرحله ۱ هر {POLL_INTERVAL_SECONDS} ثانیه | پنجره: {WINDOW_SIZE} نمونه")
    tg_status = "فعال" if (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) else "غیرفعال"
    print(f"ارسال به تلگرام: {tg_status}\n")

    states = {}
    start_ts = time.time()

    while True:
        if MAX_RUNTIME_SECONDS and (time.time() - start_ts) >= MAX_RUNTIME_SECONDS:
            print("به سقف زمانی تنظیم‌شده رسیدیم؛ خروج تمیز.")
            break

        try:
            tickers = fetch_bulk_tickers()
        except Exception as e:
            print(f"خطا در دریافت ticker فیوچرز: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        try:
            funding_rates = fetch_bulk_funding_rates()
            funding_map = {f["symbol"]: float(f["rate"]) for f in funding_rates}
        except Exception as e:
            print(f"خطا در دریافت funding rate: {e}")
            funding_map = {}

        now_ts = time.time()
        candidates = []

        for item in tickers:
            symbol = item.get("s", "")
            if "-SWAP-" not in symbol or not symbol.endswith("USDT"):
                continue

            try:
                price = float(item.get("c", 0))
                quote_volume = float(item.get("qv", 0))
            except (TypeError, ValueError):
                continue

            if quote_volume < MIN_QUOTE_VOLUME:
                continue

            state = states.setdefault(symbol, SymbolState(WINDOW_SIZE))
            state.prices.append(price)
            state.quote_volumes.append(quote_volume)
            if symbol in funding_map:
                state.funding_rates.append(funding_map[symbol])

            if not state.is_ready():
                continue

            price_change = state.price_change_pct()
            vol_ratio = state.volume_accel_ratio()
            funding_jump = state.funding_rate_jump()
            vol_consistency = state.volume_trend_consistency()

            if abs(price_change) >= CONFIRMED_PRICE_CHANGE_PCT and vol_ratio >= CONFIRMED_VOLUME_RATIO:
                if now_ts - state.last_confirmed_alert >= ALERT_COOLDOWN_SECONDS:
                    direction = "pump" if price_change > 0 else "dump"
                    rsi_confirmed = fetch_rsi(symbol)
                    text = format_confirmed_alert(symbol, direction, price_change, vol_ratio, price, rsi_confirmed)
                    print(text + "\n" + "-" * 50)
                    send_telegram_message(text)
                    state.last_confirmed_alert = now_ts
                continue

            if (
                abs(price_change) <= PRICE_MOVE_CAP_PCT
                and vol_ratio >= EARLY_VOLUME_RATIO
                and vol_consistency >= MIN_VOLUME_TREND_CONSISTENCY
            ):
                if now_ts - state.last_early_alert >= ALERT_COOLDOWN_SECONDS:
                    candidates.append((symbol, price, price_change, vol_ratio, funding_jump))

        candidates = candidates[:MAX_CANDIDATES_PER_CYCLE]

        for symbol, price, price_change, vol_ratio, funding_jump in candidates:
            state = states[symbol]

            ob_imbalance = fetch_orderbook_imbalance(symbol)
            oi_now = fetch_open_interest(symbol)
            oi_ratio = None
            oi_delta = None
            if oi_now is not None:
                state.open_interest_history.append(oi_now)
                if len(state.open_interest_history) >= 2 and state.open_interest_history[0] > 0:
                    oi_ratio = state.open_interest_history[-1] / state.open_interest_history[0]
                    oi_delta = state.open_interest_history[-1] - state.open_interest_history[0]

            is_long_bias = (ob_imbalance or 1) >= 1
            rsi = fetch_rsi(symbol)

            score = 0
            if ob_imbalance is not None and (
                ob_imbalance >= ORDERBOOK_IMBALANCE_THRESHOLD
                or ob_imbalance <= 1 / ORDERBOOK_IMBALANCE_THRESHOLD
            ):
                score += 1
            if oi_ratio is not None and oi_delta is not None:
                oi_significant = (
                    oi_ratio >= OI_CHANGE_RATIO_THRESHOLD or oi_ratio <= 1 / OI_CHANGE_RATIO_THRESHOLD
                )
                oi_direction_aligned = (oi_delta > 0) == is_long_bias
                if oi_significant and oi_direction_aligned:
                    score += 1
            if funding_jump and funding_jump >= MIN_FUNDING_JUMP:
                score += 1
            if rsi is not None:
                if is_long_bias and rsi <= RSI_LONG_MAX:
                    score += 1
                elif not is_long_bias and rsi >= RSI_SHORT_MIN:
                    score += 1

            if score >= EARLY_WARNING_MIN_SCORE:
                text = format_early_warning(
                    symbol, price, price_change, vol_ratio, ob_imbalance, oi_ratio, funding_jump, rsi
                )
                print(text + "\n" + "-" * 50)
                send_telegram_message(text)
                state.last_early_alert = now_ts

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nمتوقف شد.")