#!/usr/bin/env python3
from __future__ import annotations

import base64
import importlib.util
import json
import os
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path

os.environ["CDNMNUS_ADMIN_PASSWORD"] = "test-password"
spec = importlib.util.spec_from_file_location("admin_web", "web/app.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
temp = tempfile.TemporaryDirectory(); mod.DB = mod.Database(Path(temp.name) / "admin.db"); mod.DB.initialize()
server = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start(); port = server.server_address[1]
auth = "Basic " + base64.b64encode(b"admin:test-password").decode()


def request(method, path, payload=None, csrf=True, authenticated=True):
    conn = HTTPConnection("127.0.0.1", port, timeout=3); headers = {}
    if authenticated: headers["Authorization"] = auth
    body = None
    if payload is not None:
        body = json.dumps(payload); headers["Content-Type"] = "application/json"
        if csrf: headers["X-CSRF-Token"] = mod.CSRF_TOKEN
    conn.request(method, path, body, headers); response = conn.getresponse(); data = response.read().decode(); conn.close()
    return response.status, data


assert request("GET", "/", authenticated=False)[0] == 401
status, page = request("GET", "/")
assert status == 200 and "Gerenciar Edges" in page and "Configuração local" in page
assert request("POST", "/api/tenants", {"id":"xui1"}, csrf=False)[0] == 403
status, data = request("POST", "/api/tenants", {"id":"xui1","name":"Um","canonical_host":"xui1.test","origin_host":"origin.test","origin_port":80,"load_balancers":[]})
assert status == 201 and json.loads(data)["tenant"]["id"] == "xui1"
assert request("POST", "/api/cnames", {"tenant_id":"xui1","hostname":"cliente.test"})[0] == 201
assert request("POST", "/api/settings/port", {"port":8443})[0] == 200
assert mod.DB.setting("web_port") == 8443
status, state = request("GET", "/api/state")
assert status == 200 and json.loads(state)["web_port"] == 8443
status, vhost = request("GET", "/api/tenants/xui1/vhost")
assert status == 200 and "cache_xui1" in json.loads(vhost)["content"]

server.shutdown(); temp.cleanup(); print("admin web/auth/csrf/config checks: OK")
