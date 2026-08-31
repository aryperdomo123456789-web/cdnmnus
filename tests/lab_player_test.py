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


if __name__ == "__main__":
    unittest.main()
