#!/usr/bin/env python3
import base64
import importlib.util
import json
import threading
from http.client import HTTPConnection

spec = importlib.util.spec_from_file_location("panel", "panel/panel.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.PANEL_PASSWORD = "test-only-password"
mod.PANEL_USER = "admin"
mod.resolve_host = lambda host: ["203.0.113.10"]
mod.apply_config = lambda config: None
server = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]

def request(method, path, body=None, auth_value=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {}
    if auth_value is not None:
        headers["Authorization"] = auth_value
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body, headers)
    response = connection.getresponse()
    data = response.read().decode()
    connection.close()
    return response.status, data

good_auth = "Basic " + base64.b64encode(b"admin:test-only-password").decode()
status, _ = request("GET", "/", auth_value=good_auth)
assert status == 200
status, data = request("POST", "/api/config", json.dumps({"upstream_host": "meetaplay.site", "upstream_port": 80, "public_host": "vps.example.com"}), good_auth)
assert status == 200 and "203.0.113.10" in data
status, data = request("POST", "/api/config", json.dumps({"upstream_host": "bad;command", "upstream_port": 80}), good_auth)
assert status == 400 and ("válido" in data or "inválido" in data), data
status, _ = request("GET", "/")
assert status == 401
server.shutdown()
print("panel HTTP auth/config checks: OK")

config = {
    "upstream_host": "meetaplay.site",
    "upstream_port": 80,
    "resolved_addresses": ["203.0.113.10"],
    "public_host": "vps.example.com",
}
include = mod.render_include(config)
for required in (
    "proxy_hide_header Location;",
    "proxy_redirect off;",
    'sub_filter "http://meetaplay.site" "http://$host";',
    'sub_filter "http://203.0.113.10" "http://$host";',
    "proxy_set_header Host meetaplay.site;",
):
    assert required in include, required
assert "\\n    sub_filter" not in include
assert "sub_filter_once off;\n    sub_filter_types" in include
assert "username=" not in include and "password=" not in include
print("rendered include safeguards: OK")
