from pressure_radar_alerts import process_pressure_webhook, reset_state_for_test


def payload(state, bar_time):
    return {
        "state": state,
        "symbol": "TSLA",
        "price": 386.42,
        "bar_time": bar_time,
        "bull_score": 2 if state == "BULL_PRESSURE" else 0,
        "bear_score": 2 if state == "BEAR_PRESSURE" else 0,
        "m5": "صاعد" if state == "BULL_PRESSURE" else "هابط" if state == "BEAR_PRESSURE" else "مختلط",
        "m3": "ضغط صاعد" if state == "BULL_PRESSURE" else "ضغط هابط" if state == "BEAR_PRESSURE" else "غير مؤكد",
        "m1": "دفع صاعد" if state == "BULL_PRESSURE" else "دفع هابط" if state == "BEAR_PRESSURE" else "مختلط",
        "rv3": 1.2,
        "watch_level": 386.0,
        "invalidation": "إغلاق 1m تحت VWAP",
        "reasons": "5m=bull/3m=pressure/1m=bull/RV3=1.20",
    }


def test_state_transition_sends_once_and_duplicate_is_muted():
    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True

    first = process_pressure_webhook(payload("BULL_PRESSURE", 1000), sender)
    duplicate = process_pressure_webhook(payload("BULL_PRESSURE", 1001), sender)
    changed = process_pressure_webhook(payload("WAIT", 1002), sender)

    assert first["status"] == "processed"
    assert duplicate["reason"] == "duplicate_state"
    assert changed["status"] == "processed"
    assert len(sent) == 2
    assert "ضغط صاعد محتمل" in sent[0]
    assert "إلغاء/انتظار" in sent[1]


def test_failed_send_is_reported():
    reset_state_for_test()
    result = process_pressure_webhook(payload("BEAR_PRESSURE", 2000), lambda _message: False)
    assert result["status"] == "failed"
    assert result["telegram"] == "failed"
