import json
import unittest

import app.service as svc
from app.core.expiry_window import WINDOWS, apply_expiry_window, expiry_confluence


class ExpiryWindowV111Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history = svc._demo_history("DIA")

    def test_all_windows_filter(self):
        counts = {}
        for mode in WINDOWS:
            frame, info = apply_expiry_window(self.history, mode)
            self.assertFalse(frame.empty, mode)
            self.assertEqual(frame["expiration_date"].nunique(), info["count"])
            counts[mode] = info["count"]
        self.assertEqual(counts["0DTE"], 1)
        self.assertGreaterEqual(counts["WEEK"], counts["0DTE"])
        self.assertGreaterEqual(counts["2W"], counts["WEEK"])
        self.assertGreaterEqual(counts["ALL"], counts["2W"])

    def test_auto_weights_decay_but_activity_aware(self):
        _, info = apply_expiry_window(self.history, "AUTO")
        weights = list(info["weights"].values())
        self.assertTrue(info["weighted"])
        self.assertGreaterEqual(max(weights), 0.99)
        self.assertTrue(all(0.08 <= w <= 1.0 for w in weights))
        self.assertGreater(weights[0], weights[-1])

    def test_expiry_confluence(self):
        c = expiry_confluence(self.history, 534.0)
        self.assertIn(c["status"], {"FUERTE", "MEDIA", "DÉBIL"})
        self.assertTrue(c["zones"])
        z = c["zones"][0]
        self.assertIn("label", z)
        self.assertIn("horizon_count", z)
        self.assertLessEqual(z["count"], z["of"])

    def test_global_state_switch_and_strict_json(self):
        # Avoid public macro network calls during the unit test.
        old_macro = svc.fetch_macro_context
        try:
            svc.fetch_macro_context = lambda *a, **k: {"series": {}, "events": [], "stress": {"label": "TEST"}}
            st = svc.PlatformState()
            st.refresh(False)
            snapshots = {}
            for mode in ["0DTE", "WEEK", "2W", "MONTH", "ALL", "AUTO"]:
                result = st.set_expiry_window(mode)
                state = result["state"]
                self.assertEqual(state["expiry_window"]["mode"], mode)
                scanner = state["scanner"]
                # v1.27.7 freshness is fail-closed: this legacy demo-state test
                # validates expiry switching, not LIVE publishability. A scanner
                # without observed timestamps must be explicitly blocked, never
                # made READY by synthetic/demo data.
                if not scanner.get("ready"):
                    self.assertTrue(scanner.get("publication_blocked"))
                    self.assertEqual(scanner.get("edge_state"), "NO EDGE")
                json.dumps(svc._jsonable(result), allow_nan=False)
                snapshots[mode] = (state["gamma_center"], state["delta_center"], state["scanner"].get("evidence_score"))
            # At least one structural output should respond to a horizon change in demo data.
            self.assertGreater(len(set(snapshots.values())), 1)
        finally:
            svc.fetch_macro_context = old_macro


if __name__ == "__main__":
    unittest.main(verbosity=2)
