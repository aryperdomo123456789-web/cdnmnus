#!/usr/bin/env python3
from __future__ import annotations

import http.client
import importlib.util
import json
import socket
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "panel" / "vod_relay.py"
SPEC = importlib.util.spec_from_file_location("vod_relay", MODULE_PATH)
assert SPEC and SPEC.loader
vod = importlib.util.module_from_spec(SPEC)
import sys
sys.modules[SPEC.name] = vod
SPEC.loader.exec_module(vod)

GLOBAL_IP = "93.184.216.34"


class FakeResponse:
    def __init__(self, status: int, headers=(), body=b"") -> None:
        self.status, self._headers, self._body = status, list(headers), body
        self._offset = 0

    def getheaders(self): return self._headers
    def getheader(self, name):
        return next((v for k, v in self._headers if k.lower() == name.lower()), None)
    def read(self, amount=None):
        if amount is None: amount = len(self._body) - self._offset
        result = self._body[self._offset:self._offset + amount]; self._offset += len(result); return result


class FakeConnection(http.client.HTTPConnection):
    def __init__(self, response, record, meta):
        self.response, self.record, self.meta = response, record, meta
    def request(self, method, path, body=None, headers=None, **kwargs):
        self.record.append((self.meta, method, path, dict(headers or {})))
    def getresponse(self): return self.response
    def close(self): pass


class RelayTest(unittest.TestCase):
    def policy(self):
        return vod.Policy("http", "xui.example", 80,
                          (vod.Seed("seed.example", frozenset(("http", "https")), frozenset((80, 443))),),
                          frozenset((80, 443)), 5)

    def factory(self, responses, record):
        def make(scheme, host, port, ip):
            return FakeConnection(responses.pop(0), record, (scheme, host, port, ip))
        return make

    def test_seed_then_unknown_https_is_followed_with_pinning_and_range(self):
        responses = [FakeResponse(302, (("Location", "http://seed.example/start?secret=x"),)),
                     FakeResponse(307, (("Location", "https://unknown.example/video.mp4?token=y"),)),
                     FakeResponse(206, (("Content-Range", "bytes 0-3/10"), ("Location", "https://leak/")), b"data")]
        record = []
        relay = vod.Relay(self.policy(), lambda host: (GLOBAL_IP,), self.factory(responses, record))
        conn, response = relay.request("GET", "/movie/u/p/7.mp4?client=z", "bytes=0-3", "etag")
        self.assertEqual(response.status, 206)
        self.assertEqual([item[0][1] for item in record], ["xui.example", "seed.example", "unknown.example"])
        self.assertEqual(record[-1][0], ("https", "unknown.example", 443, GLOBAL_IP))
        self.assertEqual(record[-1][3]["Host"], "unknown.example")
        self.assertEqual(record[-1][3]["Range"], "bytes=0-3")
        self.assertEqual(record[-1][3]["If-Range"], "etag")
        conn.close()

    def test_first_redirect_must_be_registered_seed(self):
        responses = [FakeResponse(302, (("Location", "https://attacker.example/file"),))]
        relay = vod.Relay(self.policy(), lambda host: (GLOBAL_IP,), self.factory(responses, []))
        with self.assertRaises(vod.BlockedDestination):
            relay.request("GET", "/series/u/p/1.mkv", None, None)

    def test_private_or_mixed_dns_answer_is_blocked_before_connection(self):
        called = []
        relay = vod.Relay(self.policy(), lambda host: (GLOBAL_IP, "127.0.0.1"),
                          lambda *args: called.append(args))
        with self.assertRaises(vod.BlockedDestination):
            relay.request("GET", "/movie/u/p/1.mp4", None, None)
        self.assertFalse(called)

    def test_traversal_absolute_url_and_multi_range_are_rejected(self):
        for uri in ("http://evil/movie/a", "/movie/a/%2e%2e/b", "/movie/a/%252e%252e/b"):
            with self.assertRaises(vod.InvalidRequest): vod.validate_public_uri(uri)
        with self.assertRaises(vod.InvalidRequest): vod.validate_range("bytes=0-1,4-5")

    def test_snapshot_v1_is_accepted_and_seed_remains_tenant_scoped(self):
        snapshot = {"schema_version": 1, "tenants": {"a": {
            "origin": {"host": "xui.example", "port": 80},
            "vod_hosts": [{"host": "seed.example", "port": 443}]}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tenants.json"; path.write_text(json.dumps(snapshot))
            policy = vod.load_policy(path, "a")
        self.assertEqual(policy.seeds[0].schemes, frozenset(("https",)))
        self.assertEqual(policy.seeds[0].ports, frozenset((443,)))

    def test_snapshot_rejects_invalid_schemes_and_ports(self):
        base = {"schema_version": 2, "tenants": {"a": {
            "origin": {"host": "xui.example", "port": 80},
            "vod_policy": {"seeds": [{"host": "seed.example", "schemes": ["ftp"], "ports": [80]}]}}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tenants.json"
            path.write_text(json.dumps(base))
            with self.assertRaises(ValueError): vod.load_policy(path, "a")
            base["tenants"]["a"]["vod_policy"]["seeds"][0] = {
                "host": "seed.example", "schemes": ["https"], "ports": [70000]}
            path.write_text(json.dumps(base))
            with self.assertRaises(ValueError): vod.load_policy(path, "a")

    def test_response_headers_reject_ambiguous_or_invalid_content_length(self):
        with self.assertRaises(OSError):
            vod.public_response_headers((("Content-Length", "1"), ("Content-Length", "2")))
        with self.assertRaises(OSError):
            vod.public_response_headers((("Content-Length", "-1"),))
        self.assertEqual(vod.public_response_headers((("Server", "secret"), ("Content-Length", "4"))),
                         (("Content-Length", "4"),))

    def test_ipv6_authority_is_bracketed(self):
        self.assertEqual(vod.authority_from_parts("2001:4860:4860::8888", 443, "https"),
                         "[2001:4860:4860::8888]")

    def test_https_connection_uses_pinned_ip_but_hostname_as_sni(self):
        raw_socket = object()
        wrapped_socket = object()
        context = mock.Mock(); context.wrap_socket.return_value = wrapped_socket
        with mock.patch.object(vod.socket, "create_connection", return_value=raw_socket) as dial, \
             mock.patch.object(vod.ssl, "create_default_context", return_value=context):
            conn = vod.PinnedHTTPSConnection("media.example", 443, GLOBAL_IP)
            conn.connect()
        dial.assert_called_once()
        self.assertEqual(dial.call_args.args[0], (GLOBAL_IP, 443))
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="media.example")
        self.assertIs(conn.sock, wrapped_socket)

    def test_unix_health_does_not_contact_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = str(Path(tmp) / "relay.sock")
            vod.Handler.relay = mock.Mock()
            server = vod.UnixHTTPServer(socket_path, vod.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(socket_path); client.sendall(b"GET /health HTTP/1.1\r\nHost: local\r\nConnection: close\r\n\r\n")
            response = b""
            while chunk := client.recv(4096): response += chunk
            client.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])
        self.assertTrue(response.endswith(b"ok\n"))
        vod.Handler.relay.request.assert_not_called()


if __name__ == "__main__": unittest.main()
