import unittest

import app_v18_experiments as v18


BASE_PAYLOAD = {
    "schema_version": "event-v2",
    "symbol": "TSLA",
    "timeframe": "1m",
    "bar_open_time": "1790087640000",
    "bar_close_time": "1790087700000",
    "price": 375.25,
    "vwap": 375.00,
    "resistance": 375.60,
    "support": 374.80,
    "zone_half_width": 0.08,
    "range_high": 376.00,
    "range_low": 374.50,
    "cmf": 0.12,
    "obv_bull": True,
    "obv_breakout": True,
    "relative_volume": 1.35,
    "hist": 0.10,
    "momentum": 0.25,
    "rsi": 61.0,
    "adx": 24.0,
    "bull_score": 3,
    "bear_score": 0,
    "five_minute_bias": "bull",
}


class V18ExperimentIsolationTests(unittest.TestCase):
    def setUp(self):
        self.sent_messages = []
        v18.v18_state["today"] = ""
        v18.v18_state["sent_actions"] = set()
        v18.v18_state["minute_alert_sent"] = False
        v18.v18_state["trend_alert_sent"] = False
        v18.v18_state["minute_b_alert_sent"] = False
        v18.v18_state["closing_alert_sent"] = False

    def send(self, message):
        self.sent_messages.append(message)
        return True

    def test_b_map_and_confirmation_have_research_language(self):
        result = v18.process_v18_webhook(
            {**BASE_PAYLOAD, "action": "MINUTE_B_MAP", "experiment_window": "B_10_35_11_05"},
            self.send,
        )
        self.assertEqual(result["status"], "processed")
        self.assertIn("خريطة الدقيقة B", self.sent_messages[-1])
        self.assertIn("10:35–11:05", self.sent_messages[-1])
        self.assertIn("ملاحظة بحثية فقط", self.sent_messages[-1])

        result = v18.process_v18_webhook(
            {**BASE_PAYLOAD, "action": "MINUTE_B_CALL_CONFIRM", "experiment_window": "B_10_35_11_05"},
            self.send,
        )
        self.assertEqual(result["status"], "processed")
        self.assertIn("متابعة B", self.sent_messages[-1])
        self.assertIn("ملاحظة بحثية فقط", self.sent_messages[-1])

    def test_b_allows_one_confirmation_only(self):
        first = v18.process_v18_webhook(
            {**BASE_PAYLOAD, "action": "MINUTE_B_CALL_CONFIRM"}, self.send
        )
        second = v18.process_v18_webhook(
            {
                **BASE_PAYLOAD,
                "action": "MINUTE_B_PUT_CONFIRM",
                "cmf": -0.10,
                "obv_bull": False,
                "five_minute_bias": "bear",
            },
            self.send,
        )
        self.assertEqual(first["status"], "processed")
        self.assertEqual(second["status"], "ignored")
        self.assertEqual(second["reason"], "minute_b_alert_already_sent")

    def test_a_b_and_closing_limits_are_independent(self):
        a = v18.process_v18_webhook({**BASE_PAYLOAD, "action": "MINUTE_CALL_CONFIRM"}, self.send)
        b = v18.process_v18_webhook({**BASE_PAYLOAD, "action": "MINUTE_B_CALL_CONFIRM"}, self.send)
        close = v18.process_v18_webhook({**BASE_PAYLOAD, "action": "CLOSE_BREAKOUT_CALL"}, self.send)

        self.assertEqual(a["status"], "processed")
        self.assertEqual(b["status"], "processed")
        self.assertEqual(close["status"], "processed")
        self.assertEqual(len(self.sent_messages), 3)

    def test_unknown_b_action_is_ignored(self):
        result = v18.process_v18_webhook({**BASE_PAYLOAD, "action": "MINUTE_B_UNKNOWN"}, self.send)
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "unknown_action")


if __name__ == "__main__":
    unittest.main()
