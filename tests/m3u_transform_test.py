#!/usr/bin/env python3
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from core.m3u_transform import rewrite_public_playlist, sanitize_response_headers
from core.playlist_tokens import PlaylistTokenStore


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

    def test_allows_provider_media_extension_fragment_only(self) -> None:
        result = rewrite_public_playlist(
            "#EXTM3U\nhttp://origin-lab.test/play/provider-id#.mp4\n",
            self.snapshot,
        )
        self.assertIn("http://xuilab.phpd77.com/play/provider-id#.mp4", result.body)
        with self.assertRaises(ValueError):
            rewrite_public_playlist(
                "#EXTM3U\nhttp://origin-lab.test/play/provider-id#javascript\n",
                self.snapshot,
            )

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

    def test_opaque_mode_removes_credentials_and_binds_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PlaylistTokenStore(Path(tmp) / "tokens.db")
            result = rewrite_public_playlist(
                "#EXTM3U\nhttps://origin-lab.test/user/secret/100.m3u8\n",
                dict(self.snapshot, tenant_id="xui-lab"), opaque_tokens=True,
                token_store=store, token_ttl=300,
            )
            self.assertNotIn("user", result.body)
            self.assertNotIn("secret", result.body)
            self.assertRegex(result.body, r"https://xuilab\.phpd77\.com/play/pt1_[A-Za-z0-9_-]+")
            token = result.body.strip().rsplit("/", 1)[-1]
            mapping = store.resolve(token, "xui-lab")
            self.assertEqual(mapping["internal_uri"], "/user/secret/100.m3u8")
            with self.assertRaises(PermissionError):
                store.resolve(token, "other-tenant")


if __name__ == "__main__":
    unittest.main()
