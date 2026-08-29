#!/usr/bin/env python3
"""Contrato HTTP de players VOD contra um relay inteiramente local e falso.

Nao acessa rede externa, Nginx, banco ou servicos. Os perfis representam os
padroes HTTP relevantes observados em XCIPTV e IBO Player; nao alegam executar
os aplicativos proprietarios.
"""
from __future__ import annotations

import http.client
import importlib.util
import socket
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "panel" / "vod_relay.py"
SPEC = importlib.util.spec_from_file_location("vod_relay_player_test", MODULE_PATH)
assert SPEC and SPEC.loader
vod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vod
SPEC.loader.exec_module(vod)

OBJECT_SIZE = 8 * 1024 * 1024
ENTITY_TAG = '"fixture-v1"'


class FixtureResponse:
    def __init__(self, method: str, range_value: str | None) -> None:
        self.status = 200 if range_value is None else 206
        start, end = 0, OBJECT_SIZE - 1
        if range_value:
            spec = range_value.removeprefix("bytes=")
            left, right = spec.split("-", 1)
            if not left:
                start = OBJECT_SIZE - int(right)
            else:
                start = int(left)
                end = int(right) if right else OBJECT_SIZE - 1
        self._remaining = 0 if method == "HEAD" else end - start + 1
        self._headers = [
            ("Content-Type", "video/mp4"),
            ("Content-Length", str(end - start + 1)),
            ("Accept-Ranges", "bytes"),
            ("ETag", ENTITY_TAG),
            ("Server", "must-not-leak"),
            ("Set-Cookie", "credential=must-not-leak"),
            ("Location", "https://must-not-leak.invalid/token"),
            ("X-Accel-Redirect", "/must-not-leak"),
        ]
        if range_value:
            self._headers.append(("Content-Range", f"bytes {start}-{end}/{OBJECT_SIZE}"))

    def getheaders(self):
        return self._headers

    def read(self, amount: int | None = None) -> bytes:
        amount = self._remaining if amount is None else min(amount, self._remaining)
        self._remaining -= amount
        return b"x" * amount


class FixtureConnection:
    def close(self) -> None:
        return


class RecordingRelay:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self.lock = threading.Lock()

    def request(self, method: str, uri: str, range_value: str | None, if_range: str | None):
        # Reusa os validadores reais para que a borda HTTP exercite o contrato.
        vod.validate_public_uri(uri)
        vod.validate_range(range_value)
        with self.lock:
            self.calls.append((method, uri, range_value, if_range))
        return FixtureConnection(), FixtureResponse(method, range_value)


def unix_request(socket_path: str, method: str, path: str, headers: dict[str, str] | None = None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(socket_path)
    request_headers = {"Host": "canary.invalid", "Connection": "close", **(headers or {})}
    raw = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in request_headers.items()) + "\r\n"
    sock.sendall(raw.encode("ascii"))
    response = http.client.HTTPResponse(sock, method=method)
    response.begin()
    status, response_headers, body = response.status, dict(response.getheaders()), response.read()
    sock.close()
    return status, response_headers, body


class PlayerCompatibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.socket_path = str(Path(cls.tmp.name) / "player-test.sock")
        cls.relay = RecordingRelay()
        vod.Handler.relay = cls.relay
        cls.server = vod.UnixHTTPServer(cls.socket_path, vod.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def assert_private_headers_absent(self, headers: dict[str, str]) -> None:
        lowered = {name.lower() for name in headers}
        # O BaseHTTPRequestHandler identifica o proprio relay em Server; o
        # valor Server da origem deve ser substituido, nunca propagado.
        self.assertNotEqual(headers.get("Server"), "must-not-leak")
        for forbidden in ("location", "set-cookie", "via", "x-powered-by", "x-accel-redirect"):
            self.assertNotIn(forbidden, lowered)

    def test_xciptv_open_seek_and_resume_contract(self) -> None:
        agent = {"User-Agent": "XCIPTV-compatible-contract-test"}
        status, headers, body = unix_request(self.socket_path, "GET", "/movie/u/p/1.mp4", agent)
        self.assertEqual((status, len(body)), (200, OBJECT_SIZE))
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assert_private_headers_absent(headers)

        for start in (0, 1_048_576, 7_340_032):
            expected = min(65_536, OBJECT_SIZE - start)
            status, headers, body = unix_request(
                self.socket_path, "GET", "/movie/u/p/1.mp4",
                {**agent, "Range": f"bytes={start}-{start + expected - 1}", "If-Range": ENTITY_TAG},
            )
            self.assertEqual((status, len(body)), (206, expected))
            self.assertEqual(headers.get("Content-Range"), f"bytes {start}-{start + expected - 1}/{OBJECT_SIZE}")
            self.assert_private_headers_absent(headers)

    def test_ibo_player_head_open_ended_and_suffix_seek_contract(self) -> None:
        agent = {"User-Agent": "IBOPlayer-compatible-contract-test"}
        status, headers, body = unix_request(self.socket_path, "HEAD", "/series/u/p/2.mkv", agent)
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers.get("Content-Length"), str(OBJECT_SIZE))
        for requested, expected_length, expected_range in (
            ("bytes=4194304-", 4_194_304, f"bytes 4194304-{OBJECT_SIZE - 1}/{OBJECT_SIZE}"),
            ("bytes=-65536", 65_536, f"bytes {OBJECT_SIZE - 65_536}-{OBJECT_SIZE - 1}/{OBJECT_SIZE}"),
        ):
            status, headers, body = unix_request(self.socket_path, "GET", "/series/u/p/2.mkv", {**agent, "Range": requested})
            self.assertEqual((status, len(body)), (206, expected_length))
            self.assertEqual(headers.get("Content-Range"), expected_range)
            self.assert_private_headers_absent(headers)

    def test_invalid_surface_fails_closed(self) -> None:
        for range_value in ("bytes=0-1,4-5", "items=0-1", "bytes =0-1"):
            status, headers, body = unix_request(self.socket_path, "GET", "/movie/u/p/1.mp4", {"Range": range_value})
            self.assertEqual(status, 400)
            self.assert_private_headers_absent(headers)
            self.assertNotIn(b"must-not-leak", body)
        status, headers, _ = unix_request(self.socket_path, "POST", "/movie/u/p/1.mp4")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET, HEAD")

    def test_short_moderate_concurrency(self) -> None:
        def seek(index: int) -> tuple[int, int]:
            start = (index * 131_071) % (OBJECT_SIZE - 4096)
            status, _, body = unix_request(
                self.socket_path, "GET", "/movie/u/p/load.mp4",
                {"Range": f"bytes={start}-{start + 4095}", "User-Agent": "player-load-contract-test"},
            )
            return status, len(body)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(seek, range(32)))
        self.assertEqual(results, [(206, 4096)] * 32)


if __name__ == "__main__":
    unittest.main()
