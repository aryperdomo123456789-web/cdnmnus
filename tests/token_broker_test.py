#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import threading
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location("token_broker", "panel/token_broker.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

calls = 0
mode = "allowed"

class FakeOrigin(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        global calls
        calls += 1
        time.sleep(0.05)
        self.send_response(302)
        if mode == "allowed":
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/token/new.ts")
        else:
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.end_headers()

server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOrigin)
threading.Thread(target=server.serve_forever, daemon=True).start()

tmp = tempfile.TemporaryDirectory()
config = Path(tmp.name) / "config.json"
config.write_text(json.dumps({
    "origin_host": "127.0.0.1",
    "public_host": "cdn.test",
    "load_balancers": ["127.0.0.1"],
    "ttl_seconds": 2,
}))
mod.CONFIG_PATH = config
mod.STATE = mod.BrokerState()
mod.validated_addresses = lambda host: ["127.0.0.1"]

# Testa parser e rejeicao de traversal/URL absoluta.
assert mod.safe_request_uri("/hls/10_1.ts") == "/hls/10_1.ts"
assert mod.safe_request_uri("/live/u/p/10.m3u8") == "/live/u/p/10.m3u8"
assert mod.safe_request_uri("/user/pass/10.m3u8") == "/user/pass/10.m3u8"
for bad in ("", "/movie/x", "http://evil/hls/x", "/hls/../etc/passwd"):
    try: mod.safe_request_uri(bad); raise AssertionError(bad)
    except ValueError: pass

# Usa porta dinamica apenas no fake; query_origin e substituido para testar estado.
real_query = mod.query_origin
def fake_query(uri, cfg):
    global calls
    calls += 1
    time.sleep(0.05)
    return "/__cdnmnus_resolved_lb_0/token/new.ts"
mod.query_origin = fake_query

# Singleflight: 30 concorrentes causam uma resolucao.
calls = 0
results = []
threads = [threading.Thread(target=lambda: results.append(mod.STATE.resolve("/hls/10_1.ts"))) for _ in range(30)]
for t in threads: t.start()
for t in threads: t.join()
assert len(set(results)) == 1 and calls == 1

# Force refresh ignora cache e troca uma vez.
mod.STATE.resolve("/hls/10_1.ts", force=True)
assert calls == 2

# Allowlist, porta e host malicioso sao validados pelo resolver real.
location = "http://lb.test/token/new.ts"
class FakeResponse:
    status = 302
    def getheader(self, name, default=""): return location if name == "Location" else default
    def read(self, size): return b""
class FakeConnection:
    connected_to = None
    def __init__(self, host, *args, **kwargs): FakeConnection.connected_to = host
    def request(self, *args, **kwargs): pass
    def getresponse(self): return FakeResponse()
    def close(self): pass
mod.http.client.HTTPConnection = FakeConnection
mod.validated_addresses = lambda host: ["127.0.0.1"]
cfg = {"origin_host":"origin.test", "public_host":"cdn.test", "load_balancers":["lb.test"], "ttl_seconds":2}
assert real_query("/hls/20_2.ts", cfg) == "/__cdnmnus_resolved_lb_0/token/new.ts"
assert FakeConnection.connected_to == "127.0.0.1"
location = "http://169.254.169.254/latest/meta-data/"
try: real_query("/hls/20_2.ts", cfg); raise AssertionError("SSRF aceito")
except PermissionError: pass
location = "http://lb.test:8080/token"
try: real_query("/hls/20_2.ts", cfg); raise AssertionError("porta aceita")
except ValueError: pass
location = "https://lb.test/token"
try: real_query("/hls/20_2.ts", cfg); raise AssertionError("HTTPS incompatível aceito")
except ValueError: pass
assert mod.safe_request_uri("/hls/20_2.ts?x=1").startswith("/hls/")
assert mod.safe_request_uri("/movie/u/p/99.mp4", vod=True) == "/movie/u/p/99.mp4"
try: mod.safe_request_uri("/movie/u/p/99.mp4"); raise AssertionError("VOD aceito como HLS")
except ValueError: pass

# VOD segue apenas a cadeia autorizada e termina em URI interna.
responses = [(302, "http://vod.test/token/movie"), (206, "")]
class VodResponse(FakeResponse):
    def __init__(self): self.status, self.location = responses.pop(0)
    def getheader(self, name, default=""): return self.location if name == "Location" else default
class VodConnection(FakeConnection):
    def getresponse(self): return VodResponse()
mod.http.client.HTTPConnection = VodConnection
vod_cfg = {**cfg, "vod_hosts": ["vod.test"]}
assert mod.query_vod("/movie/u/p/99.mp4", vod_cfg) == "/__cdnmnus_vod_0/token/movie"

# O destino dinâmico precisa sobreviver a uma segunda análise de URI pelo
# X-Accel-Redirect/Nginx sem perder escapes usados em caminhos assinados.
responses = [(302, "http://vod.test/start"),
             (302, "http://storage.test/signed/%2Fasset.mp4"), (206, "")]
assert mod.query_vod("/movie/u/p/100.mp4", vod_cfg) == \
       "/__cdnmnus_dynamic_vod/storage.test/signed/%252Fasset.mp4"

server.shutdown(); tmp.cleanup()
print("token broker parser/singleflight/refresh checks: OK")
