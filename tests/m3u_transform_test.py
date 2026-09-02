#!/usr/bin/env python3
from __future__ import annotations

import unittest

from core.m3u_transform import rewrite_public_playlist, sanitize_response_headers


class M3UTransformTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "canonical_host": "xuilab.phpd77.com",
            "origin_host": "origin-lab.test",
            "load_balancers": [{"host": "lb-lab.test", "port": 80}],
            "vod_hosts": [{"host": "vod-lab.test", "port": 80}],
        }

    def test_rewrites_allowed_absolute_urls_to_canonical(self) -> None:
        playlist = """#EXTM3U
#EXTINF:-1,Live
http://origin-lab.test/live/user/pass/100.ts
#EXTINF:-1,Movie
https://lb-lab.test/movie/user/pass/200.mp4?token=secret
#EXTINF:-1,Series
http://vod-lab.test/series/user/pass/300.mp4
"""
        result = rewrite_public_playlist(playlist, self.snapshot)
        self.assertIn("http://xuilab.phpd77.com/live/user/pass/100.ts", result.body)
        self.assertIn("http://xuilab.phpd77.com/movie/user/pass/200.mp4?token=secret", result.body)
        self.assertIn("http://xuilab.phpd77.com/series/user/pass/300.mp4", result.body)
        self.assertEqual(len(result.rewritten_urls), 3)

    def test_rejects_unknown_hosts_and_non_m3u(self) -> None:
        with self.assertRaises(ValueError):
            rewrite_public_playlist("#EXTM3U\nhttp://evil.test/live/1.ts\n", self.snapshot)
        with self.assertRaises(ValueError):
            rewrite_public_playlist("<html>not a playlist</html>", self.snapshot)

    def test_sanitizes_sensitive_headers(self) -> None:
        headers = {
            "Server": "hidden",
            "Set-Cookie": "token=secret",
            "Location": "https://hidden.invalid/token",
            "X-CDN-Tenant": "xui1",
            "Content-Type": "application/vnd.apple.mpegurl",
        }
        sanitized = sanitize_response_headers(headers)
        self.assertEqual(sanitized, {"Content-Type": "application/vnd.apple.mpegurl"})


if __name__ == "__main__":
    unittest.main()
