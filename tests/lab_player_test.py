#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "lab-player/scripts/test_playback_flow.py"
SPEC = importlib.util.spec_from_file_location("test_playback_flow", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LabPlayerTest(unittest.TestCase):
    def test_rebase_media_url_uses_the_selected_route(self) -> None:
        source = "https://cdn.example/movie/user/pass/42.mp4?token=x"
        self.assertEqual(
            MODULE.rebase_media_url(source, "http://192.0.2.10:80/"),
            "http://192.0.2.10:80/movie/user/pass/42.mp4?token=x",
        )

    def test_reports_expected_closed_location(self) -> None:
        self.assertEqual(MODULE.expected_nginx_location("https://cdn.example/movie/u/p/42.mp4"), "vod-relay")
        self.assertEqual(MODULE.expected_nginx_location("https://cdn.example/u/p/42.m3u8"), "broker-manifest")
        self.assertEqual(MODULE.expected_nginx_location("https://cdn.example/hls/u/42.ts"), "broker-live")

    def test_reports_redact_playlist_credentials(self) -> None:
        self.assertNotIn("domagopdproje", MODULE.redact_url(
            "https://cnxt.example/get.php?username=domagopdproje&password=secret&type=m3u_plus"
        ))
        self.assertIn("type=m3u_plus", MODULE.redact_url(
            "https://cnxt.example/get.php?username=user&password=secret&type=m3u_plus"
        ))
        safe_vod = MODULE.redact_url("https://cnxt.example/movie/domagopdproje/domagopdproje/42.mp4?token=secret")
        self.assertNotIn("domagopdproje", safe_vod)
        self.assertNotIn("secret", safe_vod)
        self.assertIn("/movie/[REDACTED]/[REDACTED]/42.mp4", safe_vod)

    def test_cname_mode_is_available(self) -> None:
        self.assertTrue(MODULE.test_dns_alias)

    def test_html_response_is_not_accepted_as_playlist(self) -> None:
        path = Path(self.id().replace(".", "_") + ".m3u8")
        try:
            path.write_text("<!doctype html><html>default</html>\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                MODULE.load_or_build_samples(path, 1, refresh_samples=True)
        finally:
            path.unlink(missing_ok=True)

    def test_public_playlist_must_use_canonical_host(self) -> None:
        path = Path(self.id().replace(".", "_") + ".m3u8")
        try:
            path.write_text("#EXTM3U\nhttp://38.46.223.77/user/pass/1.m3u8\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                MODULE.validate_public_playlist(path, "cdn.phpd77.com", {"38.46.223.77"})
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
