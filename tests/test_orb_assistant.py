import unittest
from unittest.mock import patch

import orb_assistant


def make_bars(close, high, low):
    bars = []
    for index in range(12):
        bars.append({
            "h": str(high if index == 11 else 99),
            "l": str(low if index == 11 else 96),
            "c": str(close if index == 11 else 98),
            "v": 130 if index >= 10 else 100,
        })
    return bars


class OrbBreakoutRelaxationTests(unittest.TestCase):
    def setUp(self):
        with orb_assistant._orb_lock:
            orb_assistant._orb_state.update({
                "or_high": 100.0,
                "or_low": 95.0,
                "call_alerts": 0,
                "put_alerts": 0,
            })

    def test_call_accepts_close_above_range_when_candle_retests_range(self):
        # The low returns inside the range, but the close clears ORB High by $0.30.
        bars = make_bars(close=100.40, high=101.00, low=99.80)
        with patch.object(orb_assistant, "get_snapshot", return_value={"price": 101.0, "vwap": 100.0}), \
             patch.object(orb_assistant, "get_bars", return_value=bars):
            direction, data = orb_assistant.check_breakout()
        self.assertEqual(direction, "CALL")
        self.assertEqual(data["or_high"], 100.0)

    def test_put_accepts_close_below_range_when_candle_retests_range(self):
        bars = make_bars(close=94.60, high=95.20, low=94.00)
        with patch.object(orb_assistant, "get_snapshot", return_value={"price": 94.0, "vwap": 95.0}), \
             patch.object(orb_assistant, "get_bars", return_value=bars):
            direction, data = orb_assistant.check_breakout()
        self.assertEqual(direction, "PUT")
        self.assertEqual(data["or_low"], 95.0)


if __name__ == "__main__":
    unittest.main()
