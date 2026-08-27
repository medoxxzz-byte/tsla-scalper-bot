"""Verify that app.py exposes the isolated V18 experiment route."""

import app

client = app.app.test_client()
payload = {
    "action": "CLOSE_BREAKOUT_CALL",
    "timeframe": "1m",
    "price": 351.20,
    "vwap": 350.90,
    "resistance": 351.00,
    "support": 349.80,
    "zone_half_width": 0.0,
    "range_high": 351.00,
    "range_low": 349.80,
    "cmf": 0.08,
    "obv_bull": True,
    "relative_volume": 1.10,
    "hist": 0.20,
    "momentum": 0.50,
    "bull_score": 3,
    "bear_score": 0,
    "five_minute_bias": "bull",
}

app.app_v18_experiments.v18_state["today"] = ""
app.app_v18_experiments.v18_state["sent_actions"] = set()
app.app_v18_experiments.v18_state["minute_alert_sent"] = False
app.app_v18_experiments.v18_state["closing_alert_sent"] = False

original_sender = app.send_telegram
captured = []
app.send_telegram = lambda message: captured.append(message) or True
try:
    response = client.post("/reversal_experiments", json=payload)
    assert response.status_code == 200, response.data
    body = response.get_json()
    assert body["status"] == "processed", body
    assert body["action"] == "CLOSE_BREAKOUT_CALL", body
    assert len(captured) == 1, captured
    assert "ملاحظة بحثية فقط" in captured[0], captured[0]

    v17_route = client.post("/reversal_map", json={**payload, "action": "MAP_45", "timeframe": "5m"})
    assert v17_route.status_code == 200, v17_route.data
finally:
    app.send_telegram = original_sender

print("Flask V18 route verification passed: /reversal_experiments is isolated from V17.")
