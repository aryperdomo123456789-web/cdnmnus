#!/usr/bin/env python3
from __future__ import annotations

import unittest

from playback.route_policy import pick_initial_edge, recent_trigger_count, should_switch


class PlaybackRoutePolicyTest(unittest.TestCase):
    def test_prefers_ready_edge_and_honors_exclusions(self) -> None:
        edges = [
            {"id": "edge-b", "host": "edge-b.example", "state": "ready", "pressure": 0.2},
            {"id": "edge-a", "host": "edge-a.example", "state": "ready", "pressure": 0.1},
            {"id": "edge-c", "host": "edge-c.example", "state": "draining", "pressure": 0.0},
        ]
        chosen = pick_initial_edge(edges, excluded={"edge-a.example"})
        self.assertEqual(chosen["host"], "edge-b.example")

    def test_switch_threshold_uses_recent_triggering_errors(self) -> None:
        now = 1_000.0
        events = [
            {"type": "segment_timeout", "observed_at": now - 10},
            {"type": "segment_timeout", "observed_at": now - 20},
            {"type": "playlist_timeout", "observed_at": now - 30},
            {"type": "codec_error", "observed_at": now - 40},
        ]
        self.assertEqual(recent_trigger_count(events, now=now), 3)
        self.assertTrue(should_switch({"state": "active", "switch_count": 0}, events, now=now))


if __name__ == "__main__":
    unittest.main()
