#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات هشدار زودهنگام پامپ/دامپ - بازار فیوچرز توبیت (Toobit)
-------------------------------------------------------------
مهم: هیچ ربات یا الگوریتمی نمی‌تونه با قطعیت پامپ/دامپ رو *قبل* از وقوع
پیش‌بینی کنه. این اسکریپت دنبال نشانه‌های مقدماتی می‌گرده که آماری/تجربی
اغلب *قبل* از حرکت شدید قیمت ظاهر می‌شن، و بر اساس اونا یه "هشدار احتمالی"
می‌ده - نه یه سیگنال تضمینی خرید/فروش.

منطق کار (دو مرحله‌ای، برای مدیریت محدودیت نرخ درخواست API):

  مرحله ۱ - اسکن سبک کل بازار (هر پول یک درخواست bulk):
      - قیمت و حجم معاملات ۲۴ساعته همه‌ی نمادهای فیوچرز
      - نرخ فاندینگ همه‌ی نمادها
      نمادی که هنوز قیمتش خیلی حرکت نکرده (PRICE_MOVE_CAP) ولی حجم یا
      فاندینگش داره غیرعادی جهش می‌کنه => "نامزد" مرحله ۲ می‌شه.

  مرحله ۲ - تأیید روی نامزدها (چند درخواست per-symbol، فقط برای نامزدها):
      - عدم تعادل Order Book (فشار خرید/فروش در عمق بازار)
      - تغییر Open Interest (پوزیشن‌های باز)
      این‌ها رو با هم ترکیب می‌کنه و اگه امتیاز کافی بود، هشدار زودهنگام
      می‌فرسته.

      یه هشدار "تأییدشده" هم جدا وجود داره: وقتی قیمت واقعاً شروع به
      حرکت شدید می‌کنه (یعنی پامپ/دامپ همین الان داره اتفاق می‌افته).

نحوه‌ی اجرا:
    pip install requests
    export TELEGRAM_BOT_TOKEN="توکن ربات"
    export TELEGRAM_CHAT_ID="چت‌آیدی"
    python toobit_futures_early_warning.py

تنظیمات قابل تغییر در بخش CONFIG پایین هستن.
"""

import os
import time
import requests
from collections import deque
from datetime import datetime

# ============================== CONFIG ==============================

BASE_URL = "https://api.toobit.com"

TICKER_ENDPOINT = "/quote/v1/contract/ticker/24hr"       # bulk - قیمت/حجم همه نمادها
FUNDING_ENDPOINT = "/api/v1/futures/fundingRate"          # bulk - فاندینگ همه نمادها
DEPTH_ENDPOINT = "/quote/v1/depth"                        # per-symbol - عمق بازار
OPEN_INTEREST_ENDPOINT = "/quote/v1/openInterest"         # per-symbol - پوزیشن باز

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_TO_TELEGRAM = True

# هر چند ثانیه یک‌بار مرحله ۱ (اسکن سبک کل بازار) اجرا بشه
POLL_INTERVAL_SECONDS = 20

# چند تا نمونه‌ی اخیر برای هر نماد نگه داره (پنجره‌ی بررسی روند)
WINDOW_SIZE = 8  # با POLL=20s یعنی پنجره حدود ~2.5 دقیقه

# --- آستانه‌های مرحله ۱ (شناسایی نامزد) ---
# نسبت جهش حجم معاملات نسبت به میانگین دلتاهای قبلی، برای اینکه "نامزد" بشه
EARLY_VOLUME_RATIO = 2.2
# حداکثر درصد تغییر قیمتی که تا الان مجازه (اگه بیشتر شده، یعنی دیگه دیر شده و این پیش‌بینی نیست)
PRICE_MOVE_CAP_PCT = 2.5
# حداقل حجم quote-volume 24h که یه نماد اصلا بررسی بشه (فیلتر نمادهای بی‌رمق)
MIN_QUOTE_VOLUME = 50000
# حداکثر تعداد نامزد در هر دور که وارد مرحله ۲ (بررسی سنگین‌تر) بشن
MAX_CANDIDATES_PER_CYCLE = 15

# --- آستانه‌های مرحله ۲ (تأیید) ---
# عدم تعادل عمق بازار: نسبت حجم سفارش خرید به فروش در N سطح برتر
ORDERBOOK_DEPTH_LEVELS = 20
ORDERBOOK_IMBALANCE_THRESHOLD = 1.8  # یعنی یک طرف حداقل 1.8 برابر طرف مقابل
# نسبت تغییر Open Interest نسبت به مقدار قبلی برای تایید فشار پوزیشن‌گیری
OI_CHANGE_RATIO_THRESHOLD = 1.15  # یعنی افزایش/کاهش حداقل 15%

# آستانه‌ی امتیاز ترکیبی نهایی برای ارسال "هشدار زودهنگام" (هر شرط تاییدشده 1 امتیاز)
EARLY_WARNING_MIN_SCORE = 2  # از سه شرط: orderbook imbalance, OI change, funding anomaly

# --- آستانه‌ی هشدار "تأییدشده" (پامپ/دامپ همین الان در حال وقوع است) ---
CONFIRMED_PRICE_CHANGE_PCT = 6.0
CONFIRMED_VOLUME_RATIO = 3.0

# کول‌داون هشدار برای هر نماد (ثانیه) تا اسپم نشه
ALERT_COOLDOWN_SECONDS = 15 * 60

# اگه بخوای اسکریپت بعد از مدت مشخصی خودش (تمیز) خاموش بشه (مثلا برای اجرا
# روی GitHub Actions که هر job سقف زمانی داره)، این رو با متغیر محیطی
# MAX_RUNTIME_SECONDS ست کن. مقدار 0 یعنی بدون محدودیت (اجرای همیشگی).
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

    def funding_rate_jump(self):
        """تغییر ناگهانی نرخ فاندینگ نسبت به مقدار قبلی، به عنوان نشونه فشار جهت‌دار"""
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
        print("⚠️  توکن/چت‌آیدی تلگرام ست نشده؛ پیام ارسال نشد.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10
        )
        if resp.status_code != 200:
            print(f"⚠️  ارسال تلگرام ناموفق: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"⚠️  خطای ارسال تلگرام: {e}")


def fetch_bulk_tickers():
    return http_get(TICKER_ENDPOINT)


def fetch_bulk_funding_rates():
    return http_get(FUNDING_ENDPOINT)


def fetch_orderbook_imbalance(symbol):
    """نسبت حجم سفارش خرید به فروش در N سطح برتر. >1 یعنی فشار خرید، <1 یعنی فشار فروش."""
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


def format_early_warning(symbol, price_change, vol_ratio, ob_imbalance, oi_ratio, funding_jump):
    now = datetime.now().strftime("%H:%M:%S")
    direction_hint = "خرید (احتمال پامپ)" if (ob_imbalance or 1) >= 1 else "فروش (احتمال دامپ)"
    lines = [
        f"[{now}] ⚠️ هشدار زودهنگام | {symbol}",
        f"   این یه سیگنال احتمالی «قبل از حرکت شدید» است، نه تضمینی.",
        f"   تغییر قیمت تا الان: {price_change:+.2f}% (هنوز کم)",
        f"   جهش حجم معاملات: {vol_ratio:.1f}x میانگین",
    ]
    if ob_imbalance is not None:
        lines.append(f"   عدم تعادل Order Book: {ob_imbalance:.2f} → فشار سمت {direction_hint}")
    if oi_ratio is not None:
        lines.append(f"   تغییر Open Interest: {oi_ratio:.2f}x")
    if funding_jump:
        lines.append(f"   جهش نرخ فاندینگ: {funding_jump:.5f}")
    return "\n".join(lines)


def format_confirmed_alert(symbol, direction, price_change, vol_ratio, price):
    now = datetime.now().strftime("%H:%M:%S")
    arrow = "🚀 پامپ در حال وقوع" if direction == "pump" else "🔻 دامپ در حال وقوع"
    return (
        f"[{now}] {arrow} | {symbol}\n"
        f"   تغییر قیمت: {price_change:+.2f}%\n"
        f"   نسبت جهش حجم: {vol_ratio:.1f}x میانگین\n"
        f"   آخرین قیمت: {price}"
    )


def main():
    print("شروع رصد فیوچرز توبیت برای نشانه‌های زودهنگام پامپ/دامپ ...")
    print(f"مرحله ۱ هر {POLL_INTERVAL_SECONDS} ثانیه | پنجره: {WINDOW_SIZE} نمونه")
    tg_status = "فعال ✅" if (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) else "غیرفعال ⚠️"
    print(f"ارسال به تلگرام: {tg_status}\n")

    states = {}
    start_ts = time.time()

    while True:
        if MAX_RUNTIME_SECONDS and (time.time() - start_ts) >= MAX_RUNTIME_SECONDS:
            print("⏹️  به سقف زمانی تنظیم‌شده رسیدیم؛ خروج تمیز (اجرای بعدی طبق زمان‌بندی شروع می‌شه).")
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

        # ---------- مرحله ۱: اسکن سبک ----------
        for item in tickers:
            symbol = item.get("s", "")
            # فقط قراردادهای دائمی خطی USDT (مثل BTC-SWAP-USDT)
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

            # --- هشدار تأییدشده: حرکت بزرگ همین الان در حال وقوعه ---
            if abs(price_change) >= CONFIRMED_PRICE_CHANGE_PCT and vol_ratio >= CONFIRMED_VOLUME_RATIO:
                if now_ts - state.last_confirmed_alert >= ALERT_COOLDOWN_SECONDS:
                    direction = "pump" if price_change > 0 else "dump"
                    text = format_confirmed_alert(symbol, direction, price_change, vol_ratio, price)
                    print(text + "\n" + "-" * 50)
                    send_telegram_message(text)
                    state.last_confirmed_alert = now_ts
                continue  # اگه تأیید شد دیگه نیازی به early warning نیست

            # --- نامزد مرحله ۲: هنوز قیمت خیلی حرکت نکرده ولی حجم/فاندینگ داره جهش می‌کنه ---
            if abs(price_change) <= PRICE_MOVE_CAP_PCT and vol_ratio >= EARLY_VOLUME_RATIO:
                if now_ts - state.last_early_alert >= ALERT_COOLDOWN_SECONDS:
                    candidates.append((symbol, price, price_change, vol_ratio, funding_jump))

        # محدود کردن تعداد نامزدها برای کنترل تعداد درخواست‌های per-symbol
        candidates = candidates[:MAX_CANDIDATES_PER_CYCLE]

        # ---------- مرحله ۲: تأیید روی نامزدها ----------
        for symbol, price, price_change, vol_ratio, funding_jump in candidates:
            state = states[symbol]

            ob_imbalance = fetch_orderbook_imbalance(symbol)
            oi_now = fetch_open_interest(symbol)
            oi_ratio = None
            if oi_now is not None:
                state.open_interest_history.append(oi_now)
                if len(state.open_interest_history) >= 2 and state.open_interest_history[0] > 0:
                    oi_ratio = state.open_interest_history[-1] / state.open_interest_history[0]

            score = 0
            if ob_imbalance is not None and (
                ob_imbalance >= ORDERBOOK_IMBALANCE_THRESHOLD
                or ob_imbalance <= 1 / ORDERBOOK_IMBALANCE_THRESHOLD
            ):
                score += 1
            if oi_ratio is not None and (
                oi_ratio >= OI_CHANGE_RATIO_THRESHOLD or oi_ratio <= 1 / OI_CHANGE_RATIO_THRESHOLD
            ):
                score += 1
            if funding_jump and funding_jump >= 0.001:
                score += 1

            if score >= EARLY_WARNING_MIN_SCORE:
                text = format_early_warning(
                    symbol, price_change, vol_ratio, ob_imbalance, oi_ratio, funding_jump
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
