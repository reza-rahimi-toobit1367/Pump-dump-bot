# -*- coding: utf-8 -*-
"""
بک‌تست استراتژی RSI کف/سقف + فیلتر روند MA200 روی داده‌ی تاریخی توبیت
------------------------------------------------------------------------
این اسکریپت داده‌ی کندل تاریخی رو از API توبیت می‌گیره، دقیقاً همون منطق
ربات هشدار (RSI<=30 یا >=70 + هم‌جهت با MA200) رو شبیه‌سازی می‌کنه، و
گزارش عملکرد واقعی (تعداد معامله، درصد برد، سود/زیان کل و...) رو می‌ده.

مهم: نتیجه‌ی بک‌تست گذشته هیچ تضمینی برای آینده نیست. این فقط یه ابزار
ارزیابیه، نه پیش‌بینی و نه توصیه‌ی مالی.

نحوه‌ی اجرا:
    pip install requests
    python backtest_rsi_ma200.py

نتیجه هم تو کنسول چاپ می‌شه، هم (اختیاری) به تلگرام فرستاده می‌شه اگه
TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID ست شده باشن.
"""

import os
import time
import requests
from datetime import datetime

# ============================== CONFIG ==============================

BASE_URL = "https://api.toobit.com"
KLINES_ENDPOINT = "/quote/v1/klines"

# نمادهایی که می‌خوای بک‌تست بشن (فرمت داخلی توبیت، با -SWAP-)
BACKTEST_SYMBOLS = ["BTC-SWAP-USDT", "ETH-SWAP-USDT", "SOL-SWAP-USDT"]

INTERVAL = "15m"
CANDLES_PER_REQUEST = 1000     # حداکثر مجاز هر درخواست (طبق مستندات معمولا 1000)
TOTAL_CANDLES = 8000           # حدودا ~83 روز داده با کندل 15 دقیقه‌ای

RSI_PERIOD = 14
MA_PERIOD = 200
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

STOP_LOSS_PCT = 10.0
TAKE_PROFIT_PCT = 10.0

# کارمزد تقریبی رفت‌وبرگشت (ورود+خروج) به درصد، برای واقعی‌تر شدن نتیجه
ROUND_TRIP_FEE_PCT = 0.1

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_TO_TELEGRAM = True

# ======================================================================


def http_get(path, params=None):
    url = BASE_URL + path
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_historical_klines(symbol, interval, total_candles):
    """
    چون هر درخواست محدود به CANDLES_PER_REQUEST کندله، با صفحه‌بندی
    رو به عقب (endTime) داده‌ی بیشتری جمع می‌کنیم.
    """
    all_candles = []
    end_time = None

    while len(all_candles) < total_candles:
        params = {"symbol": symbol, "interval": interval, "limit": CANDLES_PER_REQUEST}
        if end_time is not None:
            params["endTime"] = end_time

        try:
            batch = http_get(KLINES_ENDPOINT, params)
        except Exception as e:
            print(f"  خطا در گرفتن کندل‌های {symbol}: {e}")
            break

        if not batch:
            break

        all_candles = batch + all_candles
        earliest_open_time = batch[0][0]
        end_time = earliest_open_time - 1

        if len(batch) < CANDLES_PER_REQUEST:
            break  # به ابتدای داده‌های موجود رسیدیم

        time.sleep(0.2)  # ادب در برابر API

    # حذف تکراری‌ها و مرتب‌سازی بر اساس زمان
    seen = set()
    unique_sorted = []
    for c in sorted(all_candles, key=lambda x: x[0]):
        if c[0] not in seen:
            seen.add(c[0])
            unique_sorted.append(c)

    return unique_sorted[-total_candles:]


def calculate_rsi_series(closes, period=14):
    """RSI به روش Wilder، برای کل سری (برمی‌گردونه لیستی هم‌طول closes، با None برای نقاط ناکافی)"""
    n = len(closes)
    rsi_values = [None] * n
    if n < period + 1:
        return rsi_values

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = max(change, 0)
        losses[i] = max(-change, 0)

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    rsi_values[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi_values[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    return rsi_values


def calculate_ma_series(closes, period=200):
    n = len(closes)
    ma_values = [None] * n
    if n < period:
        return ma_values
    window_sum = sum(closes[:period])
    ma_values[period - 1] = window_sum / period
    for i in range(period, n):
        window_sum += closes[i] - closes[i - period]
        ma_values[i] = window_sum / period
    return ma_values


def simulate_strategy(candles):
    """
    شبیه‌سازی معاملات روی سری کندل‌ها با منطق:
    RSI<=30 و قیمت>=MA200 -> لانگ | RSI>=70 و قیمت<=MA200 -> شورت
    ورود در کندل بعد از سیگنال (open اون کندل)، خروج با اولین برخورد
    به حد سود یا حد ضرر (بررسی high/low هر کندل بعدی).
    """
    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    opens = [float(c[1]) for c in candles]
    times = [int(c[0]) for c in candles]

    rsi_series = calculate_rsi_series(closes, RSI_PERIOD)
    ma_series = calculate_ma_series(closes, MA_PERIOD)

    trades = []
    i = max(RSI_PERIOD, MA_PERIOD)
    n = len(candles)

    while i < n - 1:
        rsi = rsi_series[i]
        ma = ma_series[i]
        if rsi is None or ma is None:
            i += 1
            continue

        is_long = None
        if rsi <= RSI_OVERSOLD and closes[i] >= ma:
            is_long = True
        elif rsi >= RSI_OVERBOUGHT and closes[i] <= ma:
            is_long = False

        if is_long is None:
            i += 1
            continue

        entry_idx = i + 1
        entry_price = opens[entry_idx]
        if is_long:
            stop_price = entry_price * (1 - STOP_LOSS_PCT / 100)
            target_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)
        else:
            stop_price = entry_price * (1 + STOP_LOSS_PCT / 100)
            target_price = entry_price * (1 - TAKE_PROFIT_PCT / 100)

        outcome = None
        exit_price = None
        exit_idx = None
        for j in range(entry_idx, n):
            hi, lo = highs[j], lows[j]
            if is_long:
                hit_stop = lo <= stop_price
                hit_target = hi >= target_price
            else:
                hit_stop = hi >= stop_price
                hit_target = lo <= target_price

            # اگه هر دو تو یه کندل اتفاق بیفتن، محافظه‌کارانه فرض می‌کنیم حد ضرر زودتر خورده
            if hit_stop and hit_target:
                outcome, exit_price, exit_idx = "loss", stop_price, j
                break
            elif hit_stop:
                outcome, exit_price, exit_idx = "loss", stop_price, j
                break
            elif hit_target:
                outcome, exit_price, exit_idx = "win", target_price, j
                break

        if outcome is None:
            # معامله تا آخر داده باز مونده (بی‌نتیجه) - نادیده می‌گیریم
            i += 1
            continue

        pnl_pct = TAKE_PROFIT_PCT if outcome == "win" else -STOP_LOSS_PCT
        pnl_pct -= ROUND_TRIP_FEE_PCT

        trades.append({
            "direction": "long" if is_long else "short",
            "entry_time": times[entry_idx],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "outcome": outcome,
            "pnl_pct": pnl_pct,
            "rsi_at_signal": rsi,
        })

        i = exit_idx + 1  # بعد از بسته‌شدن معامله، دنبال سیگنال بعدی بگرد (بدون هم‌پوشانی)

    return trades


def summarize(symbol, trades):
    if not trades:
        return f"📊 {symbol}: هیچ معامله‌ای در این بازه شکل نگرفت."

    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    win_rate = len(wins) / len(trades) * 100
    total_pnl = sum(t["pnl_pct"] for t in trades)
    avg_pnl = total_pnl / len(trades)

    longs = [t for t in trades if t["direction"] == "long"]
    shorts = [t for t in trades if t["direction"] == "short"]

    # بزرگ‌ترین رشته‌ی ضررهای پشت سر هم
    max_consec_loss = 0
    cur = 0
    for t in trades:
        if t["outcome"] == "loss":
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0

    lines = [
        f"📊 *{symbol}*",
        f"تعداد کل معاملات: {len(trades)} (لانگ: {len(longs)} | شورت: {len(shorts)})",
        f"درصد برد: {win_rate:.1f}% ({len(wins)} برد / {len(losses)} باخت)",
        f"مجموع سود/زیان (جمع درصدها): {total_pnl:+.1f}%",
        f"میانگین سود/زیان هر معامله: {avg_pnl:+.2f}%",
        f"بیشترین باخت پشت‌سرهم: {max_consec_loss}",
    ]
    return "\n".join(lines)


def send_telegram_message(text):
    if not SEND_TO_TELEGRAM or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"خطای ارسال تلگرام: {e}")


def main():
    print("شروع بک‌تست استراتژی RSI کف/سقف + MA200 ...")
    print(f"نمادها: {', '.join(BACKTEST_SYMBOLS)}")
    print(f"بازه: حدود {TOTAL_CANDLES} کندل {INTERVAL} (~{TOTAL_CANDLES * 15 // 60 // 24} روز)\n")

    all_summaries = []
    all_trades_combined = []

    for symbol in BACKTEST_SYMBOLS:
        print(f"در حال دریافت داده‌ی {symbol} ...")
        candles = fetch_historical_klines(symbol, INTERVAL, TOTAL_CANDLES)
        print(f"  {len(candles)} کندل دریافت شد.")

        if len(candles) < MA_PERIOD + RSI_PERIOD + 10:
            print(f"  داده‌ی کافی برای {symbol} نیست، رد شد.\n")
            continue

        trades = simulate_strategy(candles)
        all_trades_combined.extend(trades)
        summary = summarize(symbol, trades)
        print(summary + "\n" + "-" * 50)
        all_summaries.append(summary)

    # خلاصه‌ی کلی روی همه‌ی نمادها با هم
    overall = summarize("مجموع همه نمادها", all_trades_combined)
    print(overall)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    report = f"🧪 نتیجه بک‌تست RSI+MA200 | {now}\n\n" + "\n\n".join(all_summaries) + "\n\n" + overall
    send_telegram_message(report)


if __name__ == "__main__":
    main()
