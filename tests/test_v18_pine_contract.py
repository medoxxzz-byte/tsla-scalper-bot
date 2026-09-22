import unittest
from pathlib import Path


PINE_PATH = Path(__file__).resolve().parents[1] / "tm_reversal_experiments_v18.pine"


class V18PineContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PINE_PATH.read_text(encoding="utf-8")

    def test_official_identity_fields_are_present(self):
        for field in (
            '"schema_version":"event-v2"',
            '"symbol":"',
            '"timeframe":"1m"',
            '"bar_open_time":',
            '"bar_close_time":',
        ):
            self.assertIn(field, self.source)

    def test_b_window_has_its_own_map_and_actions(self):
        self.assertIn("minuteBMapAnchor = nyHour == 10 and nyMin == 34", self.source)
        self.assertIn("minuteBWindow    = (nyHour == 10 and nyMin >= 35) or (nyHour == 11 and nyMin < 5)", self.source)
        self.assertIn('f_emitEvent("MINUTE_B_MAP", minuteBResistance, minuteBSupport, minuteBHalfWidth', self.source)
        self.assertIn('f_emitEvent("MINUTE_B_CALL_CONFIRM", minuteBResistance, minuteBSupport, minuteBHalfWidth', self.source)
        self.assertIn('f_emitEvent("MINUTE_B_PUT_CONFIRM", minuteBResistance, minuteBSupport, minuteBHalfWidth', self.source)
        self.assertIn("f_emitEvent(_action, _resistance, _support, _width, _rangeHigh, _rangeLow) =>", self.source)
        self.assertIn("alert(f_payload(_action, _resistance, _support, _width, _rangeHigh, _rangeLow)", self.source)

    def test_b_confirmation_cannot_fire_on_its_map_bar(self):
        self.assertIn("minuteBMapCloseTime := time_close", self.source)
        self.assertIn("minuteBReady = minuteBMapSent and not na(minuteBMapCloseTime) and time_close > minuteBMapCloseTime", self.source)
        self.assertIn("minuteBCallConfirm = minuteBWindow and minuteBReady", self.source)
        self.assertIn("minuteBPutConfirm = minuteBWindow and minuteBReady", self.source)

    def test_b_confirmation_has_a_daily_cap_separate_from_a(self):
        self.assertIn("var bool minuteBAlertSent = false", self.source)
        self.assertIn("minuteBAlertSent := false", self.source)
        self.assertIn("if barstate.isconfirmed and isRegular and not minuteBAlertSent", self.source)
        self.assertIn("minuteBAlertSent := true", self.source)

    def test_b_payload_is_labeled_as_a_separate_window(self):
        self.assertIn('"B_10_35_11_05"', self.source)
        self.assertIn('"experiment_window"', self.source)


if __name__ == "__main__":
    unittest.main()
