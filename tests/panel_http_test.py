#!/usr/bin/env python3
import base64
import importlib.util
import json
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path

spec = importlib.util.spec_from_file_location("panel", "panel/panel.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
tmp = tempfile.TemporaryDirectory()
mod.DB_PATH = Path(tmp.name) / "panel.db"
mod.NGINX_INCLUDE = Path(tmp.name) / "upstream.conf"
mod.PANEL_PASSWORD = "12345678"
mod.PANEL_USER = "mago@dono.com"
mod.resolve_host = lambda host: ["203.0.113.10"]
mod.apply_config = lambda config: mod.save_config(config)
mod.initialize_db()
# A restauração deve remover chaves criadas por uma tentativa posterior.
mod.replace_config({"original": True})
mod.save_config({"temporaria": True})
mod.replace_config({"original": True})
assert mod.config_from_db() == {"original": True}
server = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]

def request(method, path, body=None, auth_value=None, content_type="application/json"):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {}
    if auth_value is not None:
        headers["Authorization"] = auth_value
    if body is not None:
        headers["Content-Type"] = content_type
    connection.request(method, path, body, headers)
    response = connection.getresponse()
    data = response.read().decode()
    connection.close()
    return response.status, data

good_auth = "Basic " + base64.b64encode(b"mago@dono.com:12345678").decode()
status, data = request("GET", "/api/config", auth_value=good_auth)
assert status == 200 and '"must_change": true' in data
status, page = request("GET", "/", auth_value=good_auth)
assert status == 200 and "fetch('./api/config'" in page and "fetch('./api/password'" in page
status, _ = request("GET", "/nao-existe", auth_value=good_auth)
assert status == 404
status, _ = request("POST", "/api/config", "{}", good_auth, "text/plain")
assert status == 400
status, data = request("POST", "/api/config", json.dumps({"upstream_host": "meetaplay.site", "upstream_port": 80, "public_host": "cdn.phpd77.com"}), good_auth)
assert status == 403 and "troque a senha" in data
status, data = request("POST", "/api/password", json.dumps({"current_password": "12345678", "new_password": "nova-senha-segura-2026"}), good_auth)
assert status == 200 and "senha alterada" in data
new_auth = "Basic " + base64.b64encode(b"mago@dono.com:nova-senha-segura-2026").decode()
status, data = request("POST", "/api/config", json.dumps({"upstream_host": "meetaplay.site", "upstream_port": 80, "public_host": "cdn.phpd77.com"}), new_auth)
assert status == 200 and '"ok": true' in data
status, _ = request("GET", "/")
assert status == 401

rendered = mod.render_include({
    "upstream_host": "origin.test", "resolved_addresses": ["203.0.113.10"],
    "upstream_port": 80, "public_host": "cdn.test", "load_balancers": ["lb.test"],
})
assert "map $request_uri $cdnmnus_skip_hls_cache" in rendered
assert rendered.count("proxy_no_cache $cdnmnus_skip_hls_cache;") == 5
assert "servicedovod.lat" in rendered
assert "upstream cdnmnus_vod_0" in rendered
assert "fragrant-harbor-683b.2dzncf9igp3u.workers.dev" not in rendered
assert "cdnmnus_vod_storage" not in rendered

mod.VOD_SEED_HOSTS = "vod-a.test, vod-b.test, vod-a.test"
assert mod.configured_vod_hosts() == ["vod-a.test", "vod-b.test"]
custom_vod = mod.render_include({
    "upstream_host": "origin.test", "resolved_addresses": ["203.0.113.10"],
    "upstream_port": 80, "public_host": "cdn.test", "load_balancers": [],
})
assert "server vod-a.test:80" in custom_vod
assert "server vod-b.test:80" in custom_vod
assert "location ^~ /__cdnmnus_vod_1/" in custom_vod

mod.VOD_SEED_HOSTS = "https://invalid.test"
try:
    mod.configured_vod_hosts()
except ValueError:
    pass
else:
    raise AssertionError("VOD seed with a URL scheme must be rejected")
server.shutdown()
tmp.cleanup()
print("panel SQLite/auth/password/config checks: OK")
