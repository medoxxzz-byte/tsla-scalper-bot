"""TM Reversal Map V17 — webhook formatting and exit reminders.

V17 intentionally sends a small, ordered set of map messages. It does not
send continuous momentum pulses and it never suggests averaging a position.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

V17_EXPERIMENT_GUARD = (
    "\n━━━━━━━━━━━━━━\n"
    "🧱 تجربة منضبطة: عقد واحد فقط | سعر العقد $80–$100\n"
    "🚫 لا تعزيز في الخسارة ولا تعبئة فورية للمحفظة."
)

v17_state = {
    "today": "",
    "sent_actions": set(),
    "zone_alert_sent": False,
    "exit_900_sent": False,
    "exit_925_sent": False,
}


def get_ksa_now():
    """Return Saudi time without relying on host timezone."""
    return datetime.now(timezone.utc) + timedelta(hours=3)


def _reset_daily_state(now_ksa):
    today = now_ksa.strftime("%Y-%m-%d")
    if v17_state["today"] != today:
        v17_state["today"] = today
        v17_state["sent_actions"] = set()
        v17_state["zone_alert_sent"] = False
        v17_state["exit_900_sent"] = False
        v17_state["exit_925_sent"] = False


def _money_flow_text(data):
    """Translate CMF/OBV/relative volume into plain Arabic, not numeric noise."""
    cmf = float(data.get("cmf", 0) or 0)
    obv_bull = bool(data.get("obv_bull", False))
    rel_vol = float(data.get("relative_volume", 1) or 1)

    if cmf > 0.05 and obv_bull:
        return "تدفق السيولة يدعم الصعود"
    if cmf < -0.05 and not obv_bull:
        return "تدفق السيولة يدعم الهبوط"
    if rel_vol < 0.75:
        return "السيولة هادئة؛ لا نبالغ في قراءة الحركة"
    return "تدفق السيولة محايد أو غير حاسم"


def _momentum_text(data):
    bull = int(data.get("bull_score", 0) or 0)
    bear = int(data.get("bear_score", 0) or 0)
    adx = float(data.get("adx", 0) or 0)

    if bull >= 2 and bull > bear:
        return "الزخم يميل للصعود"
    if bear >= 2 and bear > bull:
        return "الزخم يميل للهبوط"
    if adx < 20:
        return "الزخم ضعيف؛ احتمال النطاق أعلى"
    return "الزخم متداخل؛ لا أفضلية واضحة"


def _map_decision(data):
    """Return a conditional decision; price location remains more important than scores."""
    price = float(data.get("price", 0) or 0)
    support = data.get("support")
    resistance = data.get("resistance")
    width = float(data.get("zone_half_width", 0.15) or 0.15)
    bull = int(data.get("bull_score", 0) or 0)
    bear = int(data.get("bear_score", 0) or 0)

    support = float(support) if support is not None else None
    resistance = float(resistance) if resistance is not None else None
    near_support = support is not None and abs(price - support) <= width
    near_resistance = resistance is not None and abs(price - resistance) <= width

    if near_support and bull >= 2 and bull > bear:
        return (
            "🟢 <b>ترجيح CALL مشروط</b>",
            "السعر عند منطقة دعم، والسيولة والزخم لا يعاكسان الارتداد.",
            "انتظر إغلاق 5د فوق المنطقة؛ إذا عاد وأغلق تحتها فلا تدخل CALL."
        )
    if near_resistance and bear >= 2 and bear > bull:
        return (
            "🔴 <b>ترجيح PUT مشروط</b>",
            "السعر عند منطقة مقاومة، والسيولة والزخم لا يدعمان الاختراق.",
            "انتظر إغلاق 5د تحت المنطقة؛ إذا ثبت فوقها فلا تدخل PUT."
        )
    if near_support or near_resistance:
        return (
            "🟡 <b>اختبار منطقة</b>",
            "السعر عند منطقة مهمة لكن الإغلاق أو السيولة لم يثبتا الاتجاه بعد.",
            "لا تدخل عند اللمس؛ راقب إغلاق 5د فقط."
        )
    return (
        "⚪ <b>لا أفضلية الآن</b>",
        "السعر في منتصف المسافة أو المؤشرات غير متفقة.",
        "الصمت قرار: لا تطارد الحركة حتى يصل السعر إلى منطقة الخريطة."
    )


def _zone_decision(data, action):
    if action == "ZONE_CALL_CONFIRM":
        return (
            "🟢 <b>تنبيه منطقة — CALL مشروط</b>",
            "وصل السعر لمنطقة الخريطة وأغلق 5د بإشارة دعم من السعر والسيولة.",
            "لا تدخل إلا إذا بقي السعر فوق المنطقة؛ أي عودة وإغلاق تحتها تلغي الفكرة."
        )
    return (
        "🔴 <b>تنبيه منطقة — PUT مشروط</b>",
        "وصل السعر لمنطقة الخريطة وأغلق 5د بإشارة رفض من السعر والسيولة.",
        "لا تدخل إلا إذا بقي السعر تحت المنطقة؛ أي تثبيت فوقها يلغي الفكرة."
    )


def _format_zone(label, center, width):
    if center is None:
        return "غير متاحة"
    try:
        center = float(center)
        width = float(width)
        return f"${center - width:.2f}–${center + width:.2f}"
    except (TypeError, ValueError):
        return "غير متاحة"


def _map_title(action):
    return {
        "MAP_PREOPEN": "خريطة ما قبل الافتتاح",
        "MAP_OPEN_15": "خريطة أول 15 دقيقة",
        "MAP_45": "خريطة 45 دقيقة",
        "MAP_90": "خريطة 90 دقيقة",
    }.get(action, "خريطة انعكاسات")


def format_map_message(data):
    action = data.get("action", "")
    price = data.get("price")
    width = data.get("zone_half_width", 0.15)
    resistance = _format_zone("resistance", data.get("resistance"), width)
    support = _format_zone("support", data.get("support"), width)
    flow_text = _money_flow_text(data)
    momentum_text = _momentum_text(data)

    if action in ("ZONE_CALL_CONFIRM", "ZONE_PUT_CONFIRM"):
        header, reading, instruction = _zone_decision(data, action)
        title = "🗺️ خريطة انعكاسات | تنبيه منطقة | فريم 5د"
    else:
        header, reading, instruction = _map_decision(data)
        title = f"🗺️ {_map_title(action)} | TSLA | فريم 5د"

    price_text = f"${float(price):.2f}" if price not in (None, "", 0) else "غير متاح"
    msg = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 السعر: <code>{price_text}</code>\n"
        f"🔴 منطقة قرار علوية: <code>{resistance}</code>\n"
        f"🟢 منطقة قرار سفلية: <code>{support}</code>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{header}\n"
        f"📊 {reading}\n"
        f"🌊 {flow_text}.\n"
        f"⚡ {momentum_text}.\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>العمل:</b> {instruction}"
        f"{V17_EXPERIMENT_GUARD}"
    )
    return msg


def process_v17_webhook(data, send_telegram_func):
    """Process map webhooks. Repeated/unknown actions are acknowledged but muted."""
    now_ksa = get_ksa_now()
    _reset_daily_state(now_ksa)
    action = str(data.get("action", "")).upper()
    valid_actions = {
        "MAP_PREOPEN", "MAP_OPEN_15", "MAP_45", "MAP_90",
        "ZONE_CALL_CONFIRM", "ZONE_PUT_CONFIRM",
    }

    if action not in valid_actions:
        return {"status": "ignored", "reason": "unknown_action", "action": action}

    if action in v17_state["sent_actions"]:
        return {"status": "ignored", "reason": "duplicate_map", "action": action}

    if action.startswith("ZONE_"):
        if v17_state["zone_alert_sent"]:
            return {"status": "ignored", "reason": "zone_alert_already_sent", "action": action}
        v17_state["zone_alert_sent"] = True

    message = format_map_message(data)
    ok = send_telegram_func(message)
    if ok:
        v17_state["sent_actions"].add(action)
    return {
        "status": "processed" if ok else "failed",
        "action": action,
        "telegram": "sent" if ok else "failed",
        "message": message,
    }


def _exit_message(time_slot, weekday):
    messages = {
        0: {
            "900": "⏰ <b>9:00 مساءً</b> | بداية الأسبوع لا تحتاج بطولة. احمِ ما بقي من هدوئك وربحك؛ لا تفتح معركة جديدة.",
            "925": "🛑 <b>9:25 مساءً</b> | خمس دقائق للتصفية. الكاش أهم من الأمل؛ جهز لقطة المحفظة كاش يا بطل.",
        },
        1: {
            "900": "⏰ <b>9:00 مساءً</b> | اليوم يكفي. ربح صغير محفوظ أفضل من فكرة كبيرة مفتوحة.",
            "925": "🛑 <b>9:25 مساءً</b> | وقت الحسم: صفِّ المحفظة. لا تعطي آخر الدقائق فرصة لتأخذ منك قرارك.",
        },
        2: {
            "900": "⏰ <b>9:00 مساءً</b> | يا بطل، خفف السرعة. لا يوجد شيء يجب تعويضه الليلة.",
            "925": "🛑 <b>9:25 مساءً</b> | خمس دقائق وتغلق نافذة اليوم. اخرج كاش وارجع لبيتك سالماً.",
        },
        3: {
            "900": "⏰ <b>9:00 مساءً</b> | اربح أو اخسر صغيراً، لكن لا تترك آخر الوقت يكتب نهاية اليوم عنك.",
            "925": "🛑 <b>9:25 مساءً</b> | التصفية الآن هي قوة وليست انسحاباً. الكاش ملك.",
        },
        4: {
            "900": "⏰ <b>9:00 مساءً</b> | نهاية الأسبوع تقترب. احفظ رأس المال وخلّ السوق للأسبوع القادم.",
            "925": "🛑 <b>9:25 مساءً</b> | أغلق اليوم نظيفاً. صفِّ المحفظة وخذ صورة المعركة بانضباط.",
        },
    }
    day_messages = messages.get(weekday, messages[0])
    return day_messages[time_slot]


def exit_scheduler_loop(send_telegram_func):
    """Send only the approved 9:00 and 9:25 KSA exit reminders, Monday–Friday."""
    logger.info("V17 exit-reminder scheduler started.")
    while True:
        try:
            now_ksa = get_ksa_now()
            _reset_daily_state(now_ksa)
            if now_ksa.weekday() <= 4 and now_ksa.hour == 21:
                if now_ksa.minute < 10 and not v17_state["exit_900_sent"]:
                    send_telegram_func(_exit_message("900", now_ksa.weekday()))
                    v17_state["exit_900_sent"] = True
                elif 25 <= now_ksa.minute < 35 and not v17_state["exit_925_sent"]:
                    send_telegram_func(_exit_message("925", now_ksa.weekday()))
                    v17_state["exit_925_sent"] = True
        except Exception as exc:
            logger.exception("V17 exit scheduler error: %s", exc)
        time.sleep(30)
