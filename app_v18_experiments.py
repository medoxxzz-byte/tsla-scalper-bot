"""Formatting and anti-spam state for the temporary V18 research windows.

V18 is deliberately isolated from app_v17_update.  It never changes the
V17 five-minute map, GEX, or exit reminders.
"""

from datetime import datetime, timezone, timedelta

V18_RESEARCH_GUARD = (
    "\n━━━━━━━━━━━━━━\n"
    "🧱 التجربة لا تغيّر القاعدة: عقد واحد فقط | سعر العقد $80–$100\n"
    "🚫 لا تعزيز في الخسارة ولا تعبئة فورية للمحفظة."
)

V18_OBSERVATION_GUARD = (
    "\n━━━━━━━━━━━━━━\n"
    "🔒 هذه ملاحظة بحثية فقط: لا تفتح التطبيق ولا الصفقة.\n"
    "نقيس السلوك خمس جلسات ثم نعتمد الفكرة أو نلغيها."
)

v18_state = {
    "today": "",
    "sent_actions": set(),
    "minute_alert_sent": False,
    "trend_alert_sent": False,
    "closing_alert_sent": False,
}


def get_ksa_now():
    """Return Saudi time without relying on the Render host timezone."""
    return datetime.now(timezone.utc) + timedelta(hours=3)


def _reset_daily_state(now_ksa):
    today = now_ksa.strftime("%Y-%m-%d")
    if v18_state["today"] != today:
        v18_state["today"] = today
        v18_state["sent_actions"] = set()
        v18_state["minute_alert_sent"] = False
        v18_state["trend_alert_sent"] = False
        v18_state["closing_alert_sent"] = False


def _as_float(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _format_price(value):
    if value in (None, "", 0):
        return "غير متاح"
    return f"${_as_float(value):.2f}"


def _format_zone(center, width):
    if center is None:
        return "غير متاحة"
    center = _as_float(center)
    width = _as_float(width)
    if width <= 0:
        return f"${center:.2f}"
    return f"${center - width:.2f}–${center + width:.2f}"


def _money_flow_text(data):
    cmf = _as_float(data.get("cmf"))
    obv_bull = bool(data.get("obv_bull", False))
    relative_volume = _as_float(data.get("relative_volume"), 1.0)

    if cmf > 0 and obv_bull and relative_volume >= 1:
        return "السيولة تؤيد الصعود"
    if cmf < 0 and not obv_bull and relative_volume >= 1:
        return "السيولة تؤيد الهبوط"
    if relative_volume < 1:
        return "الحجم دون متوسط الدقيقة؛ لا نعطيه معنى أكبر من حجمه"
    return "السيولة ليست حاسمة"


def _five_minute_text(data):
    bias = str(data.get("five_minute_bias", "mixed")).lower()
    if bias == "bull":
        return "قراءة 5د متفقة مع الصعود"
    if bias == "bear":
        return "قراءة 5د متفقة مع الهبوط"
    return "قراءة 5د متداخلة؛ لا نرفع قوة الإشارة"


def _format_minute_map(data):
    price = _format_price(data.get("price"))
    width = _as_float(data.get("zone_half_width"), 0.08)
    resistance = _format_zone(data.get("resistance"), width)
    support = _format_zone(data.get("support"), width)
    return (
        "🧪 <b>خريطة الدقيقة — تجربة 5 جلسات</b> | TSLA\n"
        "━━━━━━━━━━━━━━\n"
        f"💰 السعر: <code>{price}</code>\n"
        f"🔴 منطقة مراقبة علوية: <code>{resistance}</code>\n"
        f"🟢 منطقة مراقبة سفلية: <code>{support}</code>\n"
        "━━━━━━━━━━━━━━\n"
        "⛔ <b>لا دخول عند الخريطة.</b>\n"
        "الأحمر والأخضر مناطق مراقبة، وليست PUT أو CALL.\n"
        "ينتهي دورها خلال 10:05–10:35 نيويورك بتنبيه واحد فقط، إن اكتمل المكان + إغلاق الدقيقة + موافقة 5د."
        f"{V18_RESEARCH_GUARD}"
    )


def _format_minute_confirmation(data, action):
    price = _format_price(data.get("price"))
    width = _as_float(data.get("zone_half_width"), 0.08)
    resistance = _format_zone(data.get("resistance"), width)
    support = _format_zone(data.get("support"), width)
    flow = _money_flow_text(data)
    five = _five_minute_text(data)

    if action == "MINUTE_CALL_CONFIRM":
        header = "🟢 <b>تجربة دقيقة — CALL مشروط</b>"
        reading = "وصل السعر لدعم خريطة الدقيقة وأغلقت دقيقة في جهة الارتداد."
        instruction = "إن دخلت للتجربة، يبقى السعر فوق الدعم. أي إغلاق دقيقة تحته يلغي الفكرة؛ لا تطارد إذا ابتعد السعر."
    else:
        header = "🔴 <b>تجربة دقيقة — PUT مشروط</b>"
        reading = "وصل السعر لمقاومة خريطة الدقيقة وأغلقت دقيقة في جهة الرفض."
        instruction = "إن دخلت للتجربة، يبقى السعر تحت المقاومة. أي إغلاق دقيقة فوقها يلغي الفكرة؛ لا تطارد إذا ابتعد السعر."

    return (
        "🧪 <b>فريم الدقيقة | 10:05–10:35 نيويورك</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"💰 السعر: <code>{price}</code>\n"
        f"🔴 المقاومة: <code>{resistance}</code>\n"
        f"🟢 الدعم: <code>{support}</code>\n"
        "━━━━━━━━━━━━━━\n"
        f"{header}\n"
        f"📊 {reading}\n"
        f"🌊 {flow}.\n"
        f"🧭 {five}.\n"
        "━━━━━━━━━━━━━━\n"
        f"<b>العمل:</b> {instruction}"
        f"{V18_RESEARCH_GUARD}"
    )


def _format_trend_context(data, action):
    price = _format_price(data.get("price"))
    vwap = _format_price(data.get("vwap"))
    support = _format_zone(data.get("support"), _as_float(data.get("zone_half_width"), 0.08))
    resistance = _format_zone(data.get("resistance"), _as_float(data.get("zone_half_width"), 0.08))
    five = _five_minute_text(data)

    if action == "MINUTE_TREND_BULL":
        header = "🟢 <b>بيئة ترند صاعد — تجربة دقيقة</b>"
        evidence = "السعر فوق VWAP، MACD فوق الصفر بزخم متصاعد، RSI إيجابي، وOBV مع ADX يؤيدان الحركة."
        instruction = (
            "ركز على الكول فقط ولا تطارد الصعود. انتظر رجوعاً إلى دعم خريطة الدقيقة أو VWAP، "
            "ثم إغلاق دقيقة فوقه قبل التفكير في صفقة. تجاهل PUT ما دام هذا السياق قائماً."
        )
        opposite = "أي إغلاق 5د تحت VWAP أو ضعف واضح في OBV يلغي سياق الترند."
    else:
        header = "🔴 <b>بيئة ترند هابط — تجربة دقيقة</b>"
        evidence = "السعر تحت VWAP، MACD تحت الصفر بزخم هابط، RSI ضعيف، وOBV مع ADX يؤيدان الحركة."
        instruction = (
            "ركز على البوت فقط ولا تطارد الهبوط. انتظر ارتداداً إلى مقاومة خريطة الدقيقة أو VWAP، "
            "ثم إغلاق دقيقة تحتها قبل التفكير في صفقة. تجاهل CALL ما دام هذا السياق قائماً."
        )
        opposite = "أي إغلاق 5د فوق VWAP أو تحسن واضح في OBV يلغي سياق الترند."

    return (
        "🧪 <b>فريم الدقيقة | 10:05–10:35 نيويورك</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"💰 السعر: <code>{price}</code> | VWAP: <code>{vwap}</code>\n"
        f"🟢 الدعم التجريبي: <code>{support}</code>\n"
        f"🔴 المقاومة التجريبية: <code>{resistance}</code>\n"
        "━━━━━━━━━━━━━━\n"
        f"{header}\n"
        f"📊 {evidence}\n"
        f"🧭 {five}.\n"
        "━━━━━━━━━━━━━━\n"
        f"<b>العمل:</b> {instruction}\n"
        f"⚠️ الإلغاء: {opposite}"
        f"{V18_RESEARCH_GUARD}"
    )


def _format_closing_observation(data, action):
    price = _format_price(data.get("price"))
    range_high = _format_price(data.get("range_high"))
    range_low = _format_price(data.get("range_low"))
    flow = _money_flow_text(data)
    five = _five_minute_text(data)

    if action == "CLOSE_BREAKOUT_CALL":
        header = "🟢 <b>مراقبة إغلاق السوق — اختراق علوي تجريبي</b>"
        direction = "أغلقت دقيقة فوق أعلى نطاق 15:15–15:30"
    else:
        header = "🔴 <b>مراقبة إغلاق السوق — كسر سفلي تجريبي</b>"
        direction = "أغلقت دقيقة تحت أدنى نطاق 15:15–15:30"

    return (
        "🔬 <b>بحث سلوك آخر السوق</b> | 15:30–15:55 نيويورك\n"
        "━━━━━━━━━━━━━━\n"
        f"💰 السعر: <code>{price}</code>\n"
        f"⬆️ أعلى نطاق 15:15–15:30: <code>{range_high}</code>\n"
        f"⬇️ أدنى نطاق 15:15–15:30: <code>{range_low}</code>\n"
        "━━━━━━━━━━━━━━\n"
        f"{header}\n"
        f"📊 الشرط الأول تحقق: {direction}.\n"
        f"🌊 الشرط الثاني تحقق: {flow}.\n"
        f"🧭 {five}.\n"
        f"{V18_OBSERVATION_GUARD}"
    )


def format_v18_message(data):
    action = str(data.get("action", "")).upper()
    if action == "MINUTE_MAP":
        return _format_minute_map(data)
    if action in {"MINUTE_CALL_CONFIRM", "MINUTE_PUT_CONFIRM"}:
        return _format_minute_confirmation(data, action)
    if action in {"MINUTE_TREND_BULL", "MINUTE_TREND_BEAR"}:
        return _format_trend_context(data, action)
    if action in {"CLOSE_BREAKOUT_CALL", "CLOSE_BREAKDOWN_PUT"}:
        return _format_closing_observation(data, action)
    raise ValueError(f"Unsupported V18 action: {action}")


def process_v18_webhook(data, send_telegram_func):
    """Accept only four bounded experimental event types and deduplicate them."""
    now_ksa = get_ksa_now()
    _reset_daily_state(now_ksa)
    action = str(data.get("action", "")).upper()
    valid_actions = {
        "MINUTE_MAP",
        "MINUTE_CALL_CONFIRM",
        "MINUTE_PUT_CONFIRM",
        "MINUTE_TREND_BULL",
        "MINUTE_TREND_BEAR",
        "CLOSE_BREAKOUT_CALL",
        "CLOSE_BREAKDOWN_PUT",
    }

    if action not in valid_actions:
        return {"status": "ignored", "reason": "unknown_action", "action": action}
    if action in v18_state["sent_actions"]:
        return {"status": "ignored", "reason": "duplicate_event", "action": action}

    if action in {"MINUTE_TREND_BULL", "MINUTE_TREND_BEAR"}:
        if v18_state["trend_alert_sent"]:
            return {"status": "ignored", "reason": "trend_alert_already_sent", "action": action}
        v18_state["trend_alert_sent"] = True
    if action in {"MINUTE_CALL_CONFIRM", "MINUTE_PUT_CONFIRM"}:
        if v18_state["minute_alert_sent"]:
            return {"status": "ignored", "reason": "minute_alert_already_sent", "action": action}
        v18_state["minute_alert_sent"] = True
    if action.startswith("CLOSE_"):
        if v18_state["closing_alert_sent"]:
            return {"status": "ignored", "reason": "closing_alert_already_sent", "action": action}
        v18_state["closing_alert_sent"] = True

    message = format_v18_message(data)
    ok = send_telegram_func(message)
    if ok:
        v18_state["sent_actions"].add(action)
    return {
        "status": "processed" if ok else "failed",
        "action": action,
        "telegram": "sent" if ok else "failed",
        "message": message,
    }
