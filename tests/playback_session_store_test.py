#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from playback.session_store import SessionStore


class PlaybackSessionStoreTest(unittest.TestCase):
    def test_create_and_switch_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "playback.db")
            store.initialize()
            session = store.create_session(
                "xui1",
                "canal-10",
                "live",
                [
                    {"id": "edge-a", "host": "edge-a.example", "state": "ready"},
                    {"id": "edge-b", "host": "edge-b.example", "state": "ready"},
                    {"id": "edge-c", "host": "edge-c.example", "state": "ready"},
                ],
                ttl_seconds=300,
                secret=b"secret",
            )
            self.assertEqual(session["tenant_id"], "xui1")
            self.assertEqual(session["telemetry_url"], f"/api/playback/sessions/{session['session_id']}/events")
            response = {}
            for index in range(3):
                response = store.record_event(
                    session["session_id"],
                    {
                        "event_id": f"evt-{index}",
                        "tenant_id": "xui1",
                        "channel_id": "canal-10",
                        "edge_id": session["edge_id"] if index < 2 else response.get("edge_id", session["edge_id"]),
                        "type": "segment_timeout",
                        "sequence": index + 1,
                        "observed_at": 1_000.0 + index,
                    },
                    [
                        {"id": "edge-a", "host": "edge-a.example", "state": "ready"},
                        {"id": "edge-b", "host": "edge-b.example", "state": "ready"},
                        {"id": "edge-c", "host": "edge-c.example", "state": "ready"},
                    ],
                    now=1_000.0 + index,
                    secret=b"secret",
                )
            self.assertEqual(response["action"], "switch_edge")
            self.assertNotEqual(response["edge_id"], session["edge_id"])


if __name__ == "__main__":
    unittest.main()
