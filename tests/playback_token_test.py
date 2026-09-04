#!/usr/bin/env python3
from __future__ import annotations

import unittest

from playback.token import build_claims, sign_claims, verify_token


class PlaybackTokenTest(unittest.TestCase):
    def test_sign_and_verify_roundtrip(self) -> None:
        claims = build_claims(
            session_id="ps-1",
            tenant_id="xui1",
            channel_id="canal-10",
            edge_id="edge-a",
            expires_at=2_000_000_000,
        )
        token = sign_claims(claims, secret=b"secret")
        verified = verify_token(token, secret=b"secret")
        self.assertEqual(verified["sid"], "ps-1")
        self.assertEqual(verified["tid"], "xui1")
        self.assertEqual(verified["eid"], "edge-a")


if __name__ == "__main__":
    unittest.main()
