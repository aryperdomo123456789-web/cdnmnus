#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playback.session_store import SessionStore


class PlaybackIntegrationTest(unittest.TestCase):
    def test_signed_session_resolves_to_bound_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "playback.db")
            store.initialize()
            session = store.create_session(
                "xuilab", "canal-10", "live",
                [{"id": "edge-a", "host": "edge-a.example", "state": "ready"}],
                media_uri="/play/pt1_Abcdefgh",
                public_host="xuilab.example",
                secret=b"secret",
            )
            parsed = urlsplit(session["play_url"])
            token = parse_qs(parsed.query)["token"][0]
            resolved = store.resolve_playback(
                session["session_id"], token, tenant_id="xuilab", channel_id="canal-10",
                media_type="live", secret=b"secret",
            )
            self.assertEqual(resolved["edge_id"], "edge-a")
            self.assertEqual(resolved["media_uri"], "/play/pt1_Abcdefgh")

            with self.assertRaises(PermissionError):
                store.resolve_playback(
                    session["session_id"], token, tenant_id="tvbrasil", channel_id="canal-10",
                    media_type="live", secret=b"secret",
                )

    def test_media_uri_and_unknown_event_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "playback.db")
            store.initialize()
            with self.assertRaises(ValueError):
                store.create_session(
                    "xuilab", "canal-10", "live",
                    [{"id": "edge-a", "host": "edge-a.example", "state": "ready"}],
                    media_uri="https://origin.example/secret.ts",
                )
            session = store.create_session(
                "xuilab", "canal-10", "live",
                [{"id": "edge-a", "host": "edge-a.example", "state": "ready"}],
                media_uri="/play/pt1_Abcdefgh",
            )
            with self.assertRaises(ValueError):
                store.record_event(
                    session["session_id"],
                    {"tenant_id": "xuilab", "channel_id": "canal-10", "edge_id": "edge-a", "type": "force_switch"},
                    [{"id": "edge-a", "host": "edge-a.example", "state": "ready"}],
                )


if __name__ == "__main__":
    unittest.main()
