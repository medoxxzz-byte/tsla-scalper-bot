"""Local verification for the bounded V18 experiment formatter; no Telegram network call."""

import app_v18_experiments as v18

sent_messages = []


def fake_send(message):
    sent_messages.append(message)
    return True


base = {
    "timeframe": "1m",
    "price": 350.25,
    "vwap": 350.00,
    "resistance": 350.60,
    "support": 349.80,
    "zone_half_width": 0.08,
    "range_high": 351.00,
    "range_low": 349.90,
    "cmf": 0.12,
    "obv_bull": True,
    "relative_volume": 1.35,
    "hist": 0.10,
    "momentum": 0.25,
    "bull_score": 3,
    "bear_score": 0,
    "five_minute_bias": "bull",
}

v18.v18_state["today"] = ""
v18.v18_state["sent_actions"] = set()
v18.v18_state["minute_alert_sent"] = False
v18.v18_state["closing_alert_sent"] = False

minute_map = v18.process_v18_webhook({**base, "action": "MINUTE_MAP"}, fake_send)
assert minute_map["status"] == "processed", minute_map
assert "خريطة الدقيقة" in sent_messages[-1]
assert "لا دخول عند الخريطة" in sent_messages[-1]

minute_duplicate = v18.process_v18_webhook({**base, "action": "MINUTE_MAP"}, fake_send)
assert minute_duplicate["status"] == "ignored", minute_duplicate
assert minute_duplicate["reason"] == "duplicate_event", minute_duplicate

minute_call = v18.process_v18_webhook({**base, "action": "MINUTE_CALL_CONFIRM"}, fake_send)
assert minute_call["status"] == "processed", minute_call
assert "CALL مشروط" in sent_messages[-1]
assert "قراءة 5د متفقة مع الصعود" in sent_messages[-1]

minute_put_after_call = v18.process_v18_webhook(
    {**base, "action": "MINUTE_PUT_CONFIRM", "cmf": -0.10, "obv_bull": False, "five_minute_bias": "bear"},
    fake_send,
)
assert minute_put_after_call["status"] == "ignored", minute_put_after_call
assert minute_put_after_call["reason"] == "minute_alert_already_sent", minute_put_after_call

close_breakout = v18.process_v18_webhook({**base, "action": "CLOSE_BREAKOUT_CALL"}, fake_send)
assert close_breakout["status"] == "processed", close_breakout
assert "مراقبة إغلاق السوق" in sent_messages[-1]
assert "ملاحظة بحثية فقط" in sent_messages[-1]

close_breakdown_after_breakout = v18.process_v18_webhook(
    {**base, "action": "CLOSE_BREAKDOWN_PUT", "cmf": -0.10, "obv_bull": False, "five_minute_bias": "bear"},
    fake_send,
)
assert close_breakdown_after_breakout["status"] == "ignored", close_breakdown_after_breakout
assert close_breakdown_after_breakout["reason"] == "closing_alert_already_sent", close_breakdown_after_breakout

unknown = v18.process_v18_webhook({**base, "action": "NOT_A_SIGNAL"}, fake_send)
assert unknown["status"] == "ignored", unknown
assert unknown["reason"] == "unknown_action", unknown

assert len(sent_messages) == 3, sent_messages
print("V18 experiment formatter verification passed: map + one 1m alert + one closing observation only.")
