"""Unit tests for logic.py — run: python3 -m unittest -v test_logic"""
import unittest

import logic


def sample(gw10=None, gw20=None):
    return {"readings": {"gw10": gw10 or {}, "gw20": gw20 or {}}}


class TestAsNumber(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(logic.as_number(5), 5.0)
        self.assertEqual(logic.as_number("3.5"), 3.5)
        self.assertEqual(logic.as_number(-2), -2.0)

    def test_invalid(self):
        self.assertIsNone(logic.as_number(None))
        self.assertIsNone(logic.as_number(""))
        self.assertIsNone(logic.as_number("   "))
        self.assertIsNone(logic.as_number("abc"))


class TestBatteryDir(unittest.TestCase):
    def test_uses_label_not_sign(self):
        # GW10 firmware reports discharge as negative; magnitude must stay positive.
        self.assertEqual(logic.battery_dir({"pbattery1": -1500, "battery_mode_label": "Discharge"}),
                         ("discharge", 1500.0))
        self.assertEqual(logic.battery_dir({"pbattery1": 1500, "battery_mode_label": "Discharge"}),
                         ("discharge", 1500.0))

    def test_missing(self):
        self.assertEqual(logic.battery_dir({"battery_mode_label": "Standby"}), ("standby", None))
        self.assertEqual(logic.battery_dir(None), ("", None))


class TestGridImport(unittest.TestCase):
    def test_importing(self):
        self.assertEqual(logic.grid_import_w({"meter_active_power_total": -2200}), 2200.0)

    def test_exporting_is_zero(self):
        self.assertEqual(logic.grid_import_w({"meter_active_power_total": 800}), 0.0)

    def test_missing(self):
        self.assertIsNone(logic.grid_import_w({}))


class TestPriceAdvice(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(logic.price_advice("high")["action"], "discharge")
        self.assertEqual(logic.price_advice("low")["action"], "charge")
        self.assertEqual(logic.price_advice("medium")["action"], "hold")
        self.assertEqual(logic.price_advice(None)["action"], "unknown")
        self.assertEqual(logic.price_advice("HIGH")["action"], "discharge")


class TestEnergyAndCost(unittest.TestCase):
    def test_energy_kwh(self):
        # 3600 W for 3600 s == 3.6 kWh
        self.assertAlmostEqual(logic.interval_energy_kwh(3600, 3600), 3.6)
        # 1000 W for 5 s
        self.assertAlmostEqual(logic.interval_energy_kwh(1000, 5), 1000 / 1000.0 * 5 / 3600.0)
        self.assertEqual(logic.interval_energy_kwh(None, 5), 0.0)

    def test_cost_import_positive(self):
        # importing 3600 W (meter -3600) for 3600 s at 2 CZK/kWh -> 3.6 kWh * 2 = 7.2 CZK
        self.assertAlmostEqual(logic.interval_cost_czk(-3600, 2.0, 3600), 7.2)

    def test_cost_export_negative(self):
        # exporting 3600 W (meter +3600) -> earn -> negative cost
        self.assertAlmostEqual(logic.interval_cost_czk(3600, 2.0, 3600), -7.2)

    def test_cost_missing(self):
        self.assertEqual(logic.interval_cost_czk(None, 2.0, 5), 0.0)
        self.assertEqual(logic.interval_cost_czk(-1000, None, 5), 0.0)


class TestSelfSufficiency(unittest.TestCase):
    def test_full(self):
        # all from PV+battery, no import -> 100%
        self.assertEqual(logic.self_sufficiency_pct(5000, 2000, 0), 100.0)

    def test_half(self):
        # 5000 self, 5000 grid -> 50%
        self.assertEqual(logic.self_sufficiency_pct(5000, 0, 5000), 50.0)

    def test_idle(self):
        self.assertIsNone(logic.self_sufficiency_pct(0, 0, 0))

    def test_negatives_clamped(self):
        self.assertEqual(logic.self_sufficiency_pct(-100, 1000, 0), 100.0)


class TestPhaseBreakdown(unittest.TestCase):
    def test_sums_across_inverters(self):
        r = {
            "gw10": {"meter_active_power1": -1000, "meter_active_power2": -20, "meter_active_power3": 10},
            "gw20": {"meter_active_power1": -1200, "meter_active_power2": 5, "meter_active_power3": -5},
        }
        pb = logic.phase_breakdown(r)
        self.assertEqual(pb["L1"], -2200)
        self.assertEqual(pb["dominant"], "L1")
        self.assertEqual(pb["spread"], round(max(-2200, -15, 5) - min(-2200, -15, 5)))

    def test_none_when_empty(self):
        self.assertIsNone(logic.phase_breakdown({"gw10": {}, "gw20": {}}))
        self.assertIsNone(logic.phase_breakdown({}))


class TestClassifyEnergyState(unittest.TestCase):
    def test_shuttle_gw10_to_gw20(self):
        s = sample(
            gw10={"pbattery1": -4000, "battery_mode_label": "Discharge"},
            gw20={"pbattery1": 3000, "battery_mode_label": "Charge"},
        )
        self.assertEqual(logic.classify_energy_state(s), "gw10_to_gw20")

    def test_shuttle_gw20_to_gw10(self):
        s = sample(
            gw10={"pbattery1": 3000, "battery_mode_label": "Charge"},
            gw20={"pbattery1": -4000, "battery_mode_label": "Discharge"},
        )
        self.assertEqual(logic.classify_energy_state(s), "gw20_to_gw10")

    def test_both_discharge(self):
        s = sample(
            gw10={"pbattery1": -1000, "battery_mode_label": "Discharge"},
            gw20={"pbattery1": 5000, "battery_mode_label": "Discharge"},
        )
        self.assertEqual(logic.classify_energy_state(s), "both_discharge")

    def test_below_threshold_is_normal(self):
        # discharge label but tiny power (<300 W) -> not counted -> normal
        s = sample(
            gw10={"pbattery1": -32, "battery_mode_label": "Discharge"},
            gw20={"pbattery1": 5000, "battery_mode_label": "Charge"},
        )
        self.assertEqual(logic.classify_energy_state(s), "normal")

    def test_unknown_when_missing(self):
        s = sample(gw10={"battery_mode_label": "Discharge"}, gw20={"pbattery1": 100})
        self.assertEqual(logic.classify_energy_state(s), "unknown")


class TestAssistNeeded(unittest.TestCase):
    CFG = {"gw20_soc_max": 40, "gw10_floor": 40, "soc_gap": 20,
           "gw20_discharge_w": 8000, "import_min": 400}

    def _s(self, s10, s20, b20, mode="Discharge", meter=-1000):
        return sample(
            gw10={"battery_soc": s10},
            gw20={"battery_soc": s20, "pbattery1": b20, "battery_mode_label": mode,
                  "meter_active_power_total": meter},
        )

    def test_triggers_on_clear_imbalance(self):
        # GW20 low (35), maxed discharge (12kW), GW10 much fuller (65), importing 1kW
        self.assertTrue(logic.assist_needed(self._s(65, 35, 12000), self.CFG))

    def test_not_when_gw20_not_low(self):
        self.assertFalse(logic.assist_needed(self._s(65, 50, 12000), self.CFG))

    def test_not_when_gap_small(self):
        self.assertFalse(logic.assist_needed(self._s(45, 35, 12000), self.CFG))

    def test_not_when_no_import(self):
        self.assertFalse(logic.assist_needed(self._s(65, 35, 12000, meter=100), self.CFG))

    def test_not_when_gw20_light_discharge(self):
        self.assertFalse(logic.assist_needed(self._s(65, 35, 2000), self.CFG))

    def test_none_when_missing(self):
        s = sample(gw10={}, gw20={"battery_soc": 35})
        self.assertIsNone(logic.assist_needed(s, self.CFG))


class TestActionLog(unittest.TestCase):
    def test_summary(self):
        rows = [{"operation": "set_gw10_ems_mode"}, {"operation": "set_gw10_ems_mode"},
                {"operation": "restore_gw10_ems_mode"}]
        s = logic.action_log_summary(rows)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["by_operation"]["set_gw10_ems_mode"], 2)
        self.assertEqual(len(s["recent"]), 3)

    def test_empty(self):
        s = logic.action_log_summary([])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["by_operation"], {})


class TestDownsample(unittest.TestCase):
    def test_no_reduction_when_small(self):
        self.assertEqual(logic.downsample([1, 2, 3], 10), [1, 2, 3])

    def test_reduces_and_keeps_last(self):
        pts = list(range(100))
        out = logic.downsample(pts, 10)
        self.assertEqual(len(out), 10)
        self.assertEqual(out[-1], 99)
        self.assertEqual(out[0], 0)


if __name__ == "__main__":
    unittest.main()
