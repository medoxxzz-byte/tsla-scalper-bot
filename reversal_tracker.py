"""
reversal_tracker.py — نظام تتبع وتقييم إشارات الانعكاس
========================================================
يراقب كل إشارة انعكاس (EARLY_WARNING أو ACTUAL_REVERSAL) وبعد 5 دقائق
يتحقق من سعر TSLA ويحكم: هل الإشارة كانت صادقة أم كاذبة؟
ثم يرسل تقرير التقييم للتلغرام ويحفظه في reversal_tracker_log.csv
"""

import os, csv, time, threading, logging, requests
from datetime import datetime, timezone, timedelta
import yfinance as yf

# ─── الإعدادات ───────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
REVERSAL_LOG_FILE  = "/home/ubuntu/reversal_log.csv"
TRACKER_LOG_FILE   = "/home/ubuntu/reversal_tracker_log.csv"
CHECK_AFTER_SECS   = 300   # تحقق بعد 5 دقائق من الإشارة
THRESHOLD_PCT      = 0.003 # 0.3% حركة = إشارة صادقة

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reversal_tracker")

# ─── الإشارات المنتظرة للتقييم ────────────────────────────────────────────────
_pending: dict = {}   # key = timestamp_str, value = dict(signal_info)
_lock = threading.Lock()

# ─── مساعد: الوقت الحالي ─────────────────────────────────────────────────────
def _now_et() -> datetime:
    et = timezone(timedelta(hours=-4))  # EDT
    return datetime.now(et)

# ─── مساعد: جلب سعر TSLA الحالي ─────────────────────────────────────────────
def _get_price() -> float:
    """جلب سعر TSLA — Alpaca أولاً (موثوق)، yfinance احتياطي."""
    # أولاً: Alpaca Snapshot (الأسرع والأكثر موثوقية)
    try:
        alpaca_key    = os.getenv("ALPACA_API_KEY", "PKW3OHVLGGWGYCFMTCKDB435WA")
        alpaca_secret = os.getenv("ALPACA_SECRET_KEY", "BeNQ9BiZ8t5wxDwb6Dmvd62W3i57wKj8SmdSTxjAQYYH")
        url = "https://data.alpaca.markets/v2/stocks/TSLA/snapshot"
        r = requests.get(url, headers={
            "APCA-API-KEY-ID": alpaca_key,
            "APCA-API-SECRET-KEY": alpaca_secret
        }, timeout=8)
        if r.status_code == 200:
            snap = r.json()
            price = float(snap.get("latestTrade", {}).get("p", 0))
            if price > 0:
                return price
    except Exception as e:
        logger.warning(f"[Tracker] Alpaca error: {e}")
    # ثانياً: yfinance كاحتياطي
    try:
        tkr = yf.Ticker("TSLA")
        data = tkr.history(period="1d", interval="1m")
        if not data.empty:
            return float(data["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"[Tracker] yfinance fallback error: {e}")
    return 0.0

# ─── مساعد: إرسال تلغرام ──────────────────────────────────────────────────────
def _send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("[Tracker] Telegram credentials missing")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        logger.error(f"[Tracker] Telegram send error: {e}")

# ─── مساعد: حفظ نتيجة التقييم في CSV ─────────────────────────────────────────
def _save_result(signal_ts: str, alert_type: str, signal_price: float,
                 price_5min: float, change_pct: float, verdict: str, reason: str):
    file_exists = os.path.isfile(TRACKER_LOG_FILE)
    with open(TRACKER_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "signal_timestamp", "alert_type", "signal_price",
                "price_after_5min", "change_pct", "verdict", "reason"
            ])
        writer.writerow([
            signal_ts, alert_type,
            f"{signal_price:.2f}", f"{price_5min:.2f}",
            f"{change_pct:+.3f}%", verdict, reason
        ])

# ─── تقييم إشارة بعد 5 دقائق ─────────────────────────────────────────────────
def _evaluate_signal(signal_ts: str, info: dict):
    """تُستدعى بعد 5 دقائق من الإشارة لتقييم صحتها."""
    signal_price = info["price"]
    alert_type   = info["alert_type"]
    delta        = info["delta"]
    reason       = info["reason"]

    price_now = _get_price()
    if price_now <= 0:
        logger.warning(f"[Tracker] Could not get price for evaluation of {signal_ts}")
        return

    change_pct = ((price_now - signal_price) / signal_price) * 100

    # ── الحكم: هل الإشارة صادقة؟ ───────────────────────────────────────────
    # إشارة الانعكاس = توقع هبوط السعر (خروج من CALL أو دخول PUT)
    # صادقة = السعر انخفض بأكثر من 0.3%
    # كاذبة = السعر ثبت أو ارتفع
    if change_pct <= -(THRESHOLD_PCT * 100):
        verdict = "✅ صادقة"
        verdict_en = "TRUE"
        emoji = "✅"
    elif change_pct >= (THRESHOLD_PCT * 100):
        verdict = "❌ كاذبة (السعر ارتفع)"
        verdict_en = "FALSE_UP"
        emoji = "❌"
    else:
        verdict = "⚠️ محايدة (تذبذب)"
        verdict_en = "NEUTRAL"
        emoji = "⚠️"

    # ── رسالة التلغرام ──────────────────────────────────────────────────────
    alert_label = "تحذير مبكر" if alert_type == "EARLY_WARNING" else "انعكاس فعلي"
    msg = (
        f"📊 <b>تقييم إشارة الانعكاس</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 <b>نوع الإشارة:</b> {alert_label}\n"
        f"⏰ <b>وقت الإشارة:</b> {signal_ts}\n"
        f"💰 <b>سعر الإشارة:</b> <code>${signal_price:.2f}</code>\n"
        f"💰 <b>السعر بعد 5 دقائق:</b> <code>${price_now:.2f}</code>\n"
        f"📈 <b>التغيير:</b> <code>{change_pct:+.2f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} <b>الحكم: {verdict}</b>\n"
        f"📝 <b>السبب الأصلي:</b> {reason}\n"
        f"📊 <b>Delta وقت الإشارة:</b> {delta}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    _send_telegram(msg)
    _save_result(signal_ts, alert_type, signal_price, price_now, change_pct, verdict_en, reason)
    logger.info(f"[Tracker] Evaluated {signal_ts}: {verdict_en} | Change: {change_pct:+.3f}%")

# ─── مراقبة ملف reversal_log.csv للإشارات الجديدة ────────────────────────────
def _watch_reversal_log():
    """
    يراقب ملف reversal_log.csv كل 10 ثوانٍ.
    عند اكتشاف إشارة جديدة، يجدول تقييمها بعد 5 دقائق.
    """
    last_size = 0
    known_signals = set()

    # تحميل الإشارات المعروفة مسبقاً (لتجنب إعادة تقييم القديمة)
    if os.path.isfile(TRACKER_LOG_FILE):
        with open(TRACKER_LOG_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                known_signals.add(row.get("signal_timestamp", ""))

    logger.info(f"[Tracker] Watching reversal_log.csv | Known signals: {len(known_signals)}")

    while True:
        time.sleep(10)
        try:
            if not os.path.isfile(REVERSAL_LOG_FILE):
                continue

            current_size = os.path.getsize(REVERSAL_LOG_FILE)
            if current_size == last_size:
                continue
            last_size = current_size

            # قراءة الإشارات الجديدة
            with open(REVERSAL_LOG_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts = row.get("timestamp_et", "")
                    if ts in known_signals:
                        continue
                    known_signals.add(ts)

                    # إشارة جديدة — جدول تقييمها بعد 5 دقائق
                    info = {
                        "price":      float(row.get("price", 0)),
                        "alert_type": row.get("alert_type", ""),
                        "delta":      row.get("delta", "0"),
                        "reason":     row.get("reason", ""),
                    }
                    logger.info(f"[Tracker] New signal detected: {ts} | {info['alert_type']} @ ${info['price']}")

                    # جدولة التقييم بعد 5 دقائق في thread منفصل
                    def _delayed_eval(signal_ts=ts, signal_info=info):
                        time.sleep(CHECK_AFTER_SECS)
                        _evaluate_signal(signal_ts, signal_info)

                    t = threading.Thread(target=_delayed_eval, daemon=True,
                                        name=f"eval_{ts[:16]}")
                    t.start()

        except Exception as e:
            logger.error(f"[Tracker] Watch loop error: {e}")

# ─── إنشاء تقرير أداء شامل من الملف التاريخي ────────────────────────────────
def generate_performance_report() -> str:
    """يولّد تقرير نصي بنسبة صدق الإشارات من السجل التاريخي."""
    if not os.path.isfile(TRACKER_LOG_FILE):
        return "لا يوجد سجل تاريخي بعد — سيبدأ التسجيل من الآن."

    total = 0
    true_count = 0
    false_count = 0
    neutral_count = 0
    early_true = 0
    actual_true = 0
    early_total = 0
    actual_total = 0

    with open(TRACKER_LOG_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            v = row.get("verdict", "")
            at = row.get("alert_type", "")
            if v == "TRUE":
                true_count += 1
                if at == "EARLY_WARNING":
                    early_true += 1
                else:
                    actual_true += 1
            elif v == "FALSE_UP":
                false_count += 1
            else:
                neutral_count += 1

            if at == "EARLY_WARNING":
                early_total += 1
            else:
                actual_total += 1

    if total == 0:
        return "لا توجد إشارات مقيّمة بعد."

    accuracy = (true_count / total) * 100
    early_acc = (early_true / early_total * 100) if early_total > 0 else 0
    actual_acc = (actual_true / actual_total * 100) if actual_total > 0 else 0

    report = (
        f"📊 <b>تقرير أداء إشارات الانعكاس</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>إجمالي الإشارات المقيّمة:</b> {total}\n"
        f"✅ <b>صادقة (هبط السعر):</b> {true_count} ({accuracy:.1f}%)\n"
        f"❌ <b>كاذبة (ارتفع السعر):</b> {false_count} ({false_count/total*100:.1f}%)\n"
        f"⚠️ <b>محايدة (تذبذب):</b> {neutral_count} ({neutral_count/total*100:.1f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <b>التحذير المبكر:</b> {early_true}/{early_total} ({early_acc:.1f}% صادق)\n"
        f"🚨 <b>الانعكاس الفعلي:</b> {actual_true}/{actual_total} ({actual_acc:.1f}% صادق)\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    return report

# ─── تشغيل كـ background thread ──────────────────────────────────────────────
def start_reversal_tracker():
    """يُستدعى من app.py لتشغيل نظام التتبع."""
    t = threading.Thread(target=_watch_reversal_log, daemon=True,
                         name="reversal_tracker")
    t.start()
    logger.info("[Tracker] Reversal Tracker started ✅")

# ─── تشغيل مستقل (للاختبار) ──────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("=== Reversal Tracker — Standalone Mode ===")

    # طباعة تقرير الأداء الحالي
    print(generate_performance_report())

    # تشغيل المراقبة
    start_reversal_tracker()

    # إبقاء البرنامج يعمل
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("[Tracker] Stopped.")
