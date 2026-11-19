from pressure_radar_alerts import (
    process_pressure_webhook,
    reset_state_for_test,
    support_history_snapshot,
)


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


def test_alert_id_is_independent_from_radar_state_and_deduplicated_by_event_key():
    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True
    first_payload = payload("BULL_PRESSURE", 3000)
    first_payload.update({
        "alert_id": "SUPPORT_MONITOR",
        "support_id": "support-386",
        "event_key": "TSLA:SUPPORT_MONITOR:support-386:3000",
    })
    duplicate_payload = dict(first_payload, bar_time=3001)
    duplicate_payload["event_key"] = first_payload["event_key"]

    first = process_pressure_webhook(first_payload, sender)
    duplicate = process_pressure_webhook(duplicate_payload, sender)

    assert first["status"] == "processed"
    assert duplicate["reason"] == "duplicate_support_phase"
    assert len(sent) == 1
    assert "مراقبة الدعم" in sent[0]


def test_payload_fields_render_and_keep_rv3_separate_from_execution_relative_volume():
    data = payload("BULL_PRESSURE", 4000)
    data.update({
        "alert_id": "SUPPORT_SENSITIVITY",
        "support_id": "support-386",
        "support_level": 386.0,
        "resistance_level": 390.0,
        "m5": "bull",
        "m3": "compressed",
        "m1": "bull",
        "rv3": 1.24,
        "relative_volume": 0.91,
    })
    from pressure_radar_alerts import format_pressure_message
    message = format_pressure_message(data)
    assert "5د=bull | 3د=compressed | 1د=bull" in message
    assert "RV3=1.24" in message
    assert "مستوى المراقبة: <code>$386.00</code>" in message


def test_breakout_uses_resistance_and_support_sensitivity_dedupes_semantically():
    from pressure_radar_alerts import format_pressure_message
    breakout = payload("BULL_PRESSURE", 5000)
    breakout.update({
        "alert_id": "BULLISH_BREAKOUT",
        "support_level": 386.0,
        "resistance_level": 390.0,
    })
    assert "مستوى المراقبة: <code>$390.00</code>" in format_pressure_message(breakout)

    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True
    base = payload("BULL_PRESSURE", 6000)
    base.update({
        "alert_id": "SUPPORT_SENSITIVITY",
        "support_id": "support-386",
        "event_key": "TSLA:SUPPORT_SENSITIVITY:support-386:6000",
    })
    assert process_pressure_webhook(base, sender)["status"] == "processed"
    for bar_time in (6015, 6030, 6045):
        duplicate = dict(base, bar_time=bar_time, event_key=f"TSLA:SUPPORT_SENSITIVITY:support-386:{bar_time}")
        assert process_pressure_webhook(duplicate, sender)["reason"] == "duplicate_support_phase"
    assert len(sent) == 1

    changed_state = dict(base, state="WAIT", bar_time=6060, event_key="TSLA:SUPPORT_SENSITIVITY:support-386:6060")
    assert process_pressure_webhook(changed_state, sender)["reason"] == "duplicate_support_phase"
    changed_support = dict(base, support_id="support-387", bar_time=6075, event_key="TSLA:SUPPORT_SENSITIVITY:support-387:6075")
    assert process_pressure_webhook(changed_support, sender)["status"] == "processed"
    important = dict(base, alert_id="SUPPORT_FAILURE", bar_time=6090, event_key="TSLA:SUPPORT_FAILURE:support-386:6090")
    assert process_pressure_webhook(important, sender)["status"] == "processed"
    assert len(sent) == 3


def test_support_cycle_keeps_history_and_rearms_a_new_attempt():
    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True

    def support(alert_id, bar_time):
        data = payload("BULL_PRESSURE", bar_time)
        data.update({
            "alert_id": alert_id,
            "support_id": "support-370",
            "support_level": 370.0,
            "event_key": f"TSLA:{alert_id}:support-370:{bar_time}",
        })
        return process_pressure_webhook(data, sender)

    assert support("SUPPORT_MONITOR", 7000)["cycle_id"] == 1
    assert support("SUPPORT_MONITOR", 7015)["reason"] == "duplicate_support_phase"
    assert support("SUPPORT_SENSITIVITY", 7030)["attempt_id"] == 1
    assert support("SUPPORT_FAILURE", 7045)["attempt_id"] == 1
    assert support("SUPPORT_RECOVERY", 7060)["attempt_id"] == 1
    assert support("SUPPORT_RECOVERY", 7075)["reason"] == "duplicate_support_phase"
    next_attempt = support("SUPPORT_MONITOR", 7090)
    assert next_attempt["cycle_id"] == 1
    assert next_attempt["attempt_id"] == 2
    history = support_history_snapshot()["TSLA:support-370"]
    assert [row["to_phase"] for row in history] == [
        "MONITOR", "SENSITIVITY", "FAILURE", "RECOVERY", "MONITOR"
    ]
    assert len(sent) == 5


def test_structural_breakout_dedup_remains_on_existing_path():
    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True
    data = payload("BULL_PRESSURE", 8000)
    data.update({
        "alert_id": "BULLISH_BREAKOUT",
        "event_key": "TSLA:BULLISH_BREAKOUT:8000",
        "resistance_level": 390.0,
    })
    assert process_pressure_webhook(data, sender)["status"] == "processed"
    duplicate = dict(data, bar_time=8015)
    assert process_pressure_webhook(duplicate, sender)["reason"] == "duplicate_alert"
    new_event = dict(data, bar_time=8030, event_key="TSLA:BULLISH_BREAKOUT:8030")
    assert process_pressure_webhook(new_event, sender)["status"] == "processed"
    assert len(sent) == 2


def test_bear_wait_bear_same_context_is_continuation():
    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True

    def send(state, bar_time):
        data = payload(state, bar_time)
        data.update({"pressure_context": "bear-support-386", "event_key": f"{state}:{bar_time}"})
        return process_pressure_webhook(data, sender)

    assert send("BEAR_PRESSURE", 9000)["status"] == "processed"
    assert send("WAIT", 9015)["status"] == "processed"
    assert send("BEAR_PRESSURE", 9030)["reason"] == "duplicate_pressure_context"
    assert len(sent) == 2


def test_bull_wait_bull_same_context_is_continuation():
    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True

    def send(state, bar_time):
        data = payload(state, bar_time)
        data.update({"pressure_context": "bull-support-386", "event_key": f"{state}:{bar_time}"})
        return process_pressure_webhook(data, sender)

    assert send("BULL_PRESSURE", 9100)["status"] == "processed"
    assert send("WAIT", 9115)["status"] == "processed"
    assert send("BULL_PRESSURE", 9130)["reason"] == "duplicate_pressure_context"
    assert len(sent) == 2


def test_pressure_direction_change_is_sent():
    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True
    bear = payload("BEAR_PRESSURE", 9200)
    bear.update({"pressure_context": "context-a", "event_key": "bear:9200"})
    bull = payload("BULL_PRESSURE", 9215)
    bull.update({"pressure_context": "context-a", "event_key": "bull:9215"})
    assert process_pressure_webhook(bear, sender)["status"] == "processed"
    assert process_pressure_webhook(bull, sender)["status"] == "processed"
    assert len(sent) == 2


def test_pressure_return_after_wait_with_changed_context_is_sent():
    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True
    results = []
    for state, context, bar_time in [
        ("BEAR_PRESSURE", "context-a", 9300),
        ("WAIT", "context-a", 9315),
        ("BEAR_PRESSURE", "context-b", 9330),
    ]:
        data = payload(state, bar_time)
        data.update({"pressure_context": context, "event_key": f"{context}:{bar_time}"})
        results.append(process_pressure_webhook(data, sender))
    assert results[-1]["status"] == "processed"
    assert len(sent) == 3


def test_pressure_reentry_after_wait_without_context_is_fail_closed():
    reset_state_for_test()
    sender = lambda _message: True
    assert process_pressure_webhook(payload("BEAR_PRESSURE", 9350), sender)["status"] == "processed"
    assert process_pressure_webhook(payload("WAIT", 9365), sender)["status"] == "processed"
    result = process_pressure_webhook(payload("BEAR_PRESSURE", 9380), sender)
    assert result["reason"] == "insufficient_context_for_pressure_reentry"


def test_failed_telegram_does_not_commit_and_allows_retry():
    reset_state_for_test()
    attempts = []

    def sender(message):
        attempts.append(message)
        return len(attempts) > 1

    data = payload("BEAR_PRESSURE", 9400)
    data.update({"pressure_context": "context-retry", "event_key": "retry:9400"})
    first = process_pressure_webhook(data, sender)
    second = process_pressure_webhook(dict(data, bar_time=9415, event_key="retry:9415"), sender)
    assert first["status"] == "failed"
    assert second["status"] == "processed"
    assert len(attempts) == 2


def test_context_is_conservative_without_a_valid_prior_observation():
    from pressure_radar_alerts import format_pressure_message

    data = payload("BULL_PRESSURE", 10000)
    data.update({"bull_score": 2, "bear_score": 0, "momentum": 0.2, "cmf": 0.1})
    message = format_pressure_message(data)
    assert "ضغط صاعد قائم" in message
    assert "لا توجد مقارنة زمنية كافية" in message
    assert "كأمر دخول" in message
    assert "Footprint" not in message
    assert "Delta" not in message


def test_context_compares_only_newer_observations_and_describes_real_weakening():
    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True

    first = payload("BULL_PRESSURE", 11000)
    first.update({
        "alert_id": "BULLISH_IMPROVEMENT",
        "event_key": "improvement:11000",
        "bull_score": 2,
        "bear_score": 0,
        "momentum": 0.40,
        "cmf": 0.20,
        "relative_volume": 1.20,
    })
    second = dict(first)
    second.update({
        "bar_time": 11015,
        "event_key": "improvement:11015",
        "momentum": 0.08,
        "cmf": 0.08,
        "relative_volume": 0.60,
    })

    assert process_pressure_webhook(first, sender)["status"] == "processed"
    result = process_pressure_webhook(second, sender)
    assert result["status"] == "processed"
    assert "الدفع يتراجع" in sent[-1]
    assert "لا تطارد السعر" in sent[-1]


def test_stale_or_duplicate_timestamp_is_ignored_before_comparison():
    reset_state_for_test()
    sent = []
    sender = lambda message: sent.append(message) or True

    first = payload("BULL_PRESSURE", 12000)
    first.update({
        "alert_id": "BULLISH_IMPROVEMENT",
        "event_key": "improvement:12000",
        "momentum": 0.2,
    })
    assert process_pressure_webhook(first, sender)["status"] == "processed"

    stale = dict(first, bar_time=11985, event_key="improvement:11985")
    duplicate_time = dict(first, event_key="improvement:12000:duplicate")
    assert process_pressure_webhook(stale, sender)["reason"] == "stale_observation"
    assert process_pressure_webhook(duplicate_time, sender)["reason"] == "stale_observation"
    assert len(sent) == 1


def test_wait_with_one_bear_vote_is_described_without_changing_state():
    from pressure_radar_alerts import format_pressure_message

    data = payload("WAIT", 13000)
    data.update({"bull_score": 0, "bear_score": 1, "momentum": -0.20, "cmf": -0.10})
    message = format_pressure_message(data)
    assert "ميل هابط" in message
    assert "غير مؤكد" in message
    assert "BEAR_PRESSURE" not in message


def test_relative_volume_and_cmf_are_described_as_activity_not_footprint():
    from pressure_radar_alerts import format_pressure_message

    previous = {
        "time": 14000,
        "state": "BULL_PRESSURE",
        "bull_score": 2.0,
        "bear_score": 0.0,
        "momentum": 0.30,
        "cmf": 0.20,
        "relative_volume": 1.40,
    }
    data = payload("BULL_PRESSURE", 14015)
    data.update({
        "bull_score": 2,
        "bear_score": 0,
        "momentum": 0.30,
        "cmf": 0.08,
        "relative_volume": 0.70,
    })
    message = format_pressure_message(data, previous)
    assert "النشاط النسبي تراجع" in message or "التدفق التقريبي أضعف" in message
    assert "شراءً أو بيعًا مؤكدًا" in message or "الحجم النسبي وحده" in message
    assert "Footprint" not in message
    assert "Delta" not in message
