from __future__ import annotations

import unittest

from core.capacity_policy import evaluate_capacity


PROFILE = {"capacity_mbps": 10000, "headroom": 0.25}


def sample(**overrides):
    value = {
        "tx_mbps": 1000, "cpu_pct": 20, "p95_ms": 50,
        "http5xx": 0, "nic_errors": 0, "vod_206_ok": True,
    }
    value.update(overrides)
    return value


class CapacityPolicyTest(unittest.TestCase):
    def test_equal_healthy_edges_keep_full_admission(self):
        result = evaluate_capacity(PROFILE, sample())
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["desired_weight"], 100)
        self.assertEqual(result["usable_mbps"], 7500.0)

    def test_pressure_drains_new_sessions(self):
        result = evaluate_capacity(PROFILE, sample(tx_mbps=6500))
        self.assertEqual(result["state"], "draining")
        self.assertEqual(result["desired_weight"], 0)

    def test_bad_range_or_nic_fails_closed(self):
        self.assertEqual(evaluate_capacity(PROFILE, sample(nic_errors=1))["state"], "down")
        self.assertEqual(evaluate_capacity(PROFILE, sample(vod_206_ok=False))["state"], "down")


if __name__ == "__main__":
    unittest.main()
