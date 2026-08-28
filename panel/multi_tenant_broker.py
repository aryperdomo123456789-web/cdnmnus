#!/usr/bin/env python3
"""Broker por tenant sobre Unix socket, compatível com o resolver legado."""
from __future__ import annotations

import json
import os
import re
import socketserver
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import token_broker as legacy

SNAPSHOT_PATH = Path(os.environ.get("CDNMNUS_TENANTS_CONFIG", "/opt/cdnmnus/current/broker/tenants.json"))
TENANT_ID = os.environ.get("CDNMNUS_TENANT_ID", "")
SOCKET_PATH = Path(os.environ.get("CDNMNUS_BROKER_SOCKET", f"/run/cdnmnus/broker-{TENANT_ID}.sock"))
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class TenantState:
    def __init__(self, tenant_id: str) -> None:
        if not TENANT_RE.fullmatch(tenant_id):
            raise ValueError("tenant_id inválido")
        self.tenant_id = tenant_id
        self.state = legacy.BrokerState()
        self.mtime = -1.0
        self.hosts: set[str] = set()

    def load(self) -> dict[str, object]:
        stat = SNAPSHOT_PATH.stat()
        if stat.st_mtime != self.mtime:
            snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
            if int(snapshot.get("schema_version", 0)) != 1:
                raise ValueError("schema do snapshot incompatível")
            tenant = snapshot.get("tenants", {}).get(self.tenant_id)
            if not isinstance(tenant, dict):
                raise ValueError("tenant ausente no snapshot")
            public_hosts = [str(x).lower() for x in tenant.get("public_hosts", [])]
            origin = tenant.get("origin", {})
            if not public_hosts or not isinstance(origin, dict):
                raise ValueError("tenant incompleto")
            lbs = tenant.get("load_balancers", [])
            vod = tenant.get("vod_hosts", [])
            config: dict[str, object] = {
                "origin_host": str(origin.get("host", "")),
                "public_host": public_hosts[0],
                "load_balancers": [str(x["host"] if isinstance(x, dict) else x) for x in lbs],
                "vod_hosts": [str(x["host"] if isinstance(x, dict) else x) for x in vod],
                "ttl_seconds": int(tenant.get("ttl_seconds", 15)),
            }
            if int(origin.get("port", 80)) != 80:
                raise ValueError("porta da origem ainda não suportada")
            self.hosts = set(public_hosts)
            self.state.config = config
            self.state.config_mtime = stat.st_mtime
            self.mtime = stat.st_mtime
        return self.state.config

    def validate_request(self, tenant_id: str, public_host: str) -> None:
        self.load()
        host = public_host.split(":", 1)[0].lower().rstrip(".")
        if tenant_id != self.tenant_id or host not in self.hosts:
            raise PermissionError("tenant/hostname divergente")

    def resolve(self, uri: str, force: bool, vod: bool) -> str:
        config = self.load()
        self.state.load_config = lambda: config  # type: ignore[method-assign]
        internal = self.state.resolve(uri, force=force, vod=vod)
        mappings = {
            "/__cdnmnus_resolved_origin": f"/__cdnmnus_{self.tenant_id}_origin",
            "/__cdnmnus_retry_origin": f"/__cdnmnus_{self.tenant_id}_origin",
        }
        for source, target in mappings.items():
            if internal.startswith(source):
                return target + internal[len(source):]
        for kind in ("resolved_lb_", "retry_lb_", "vod_", "vod_retry_"):
            source = f"/__cdnmnus_{kind}"
            if internal.startswith(source):
                normalized = "lb_" if "lb_" in kind else "vod_"
                return f"/__cdnmnus_{self.tenant_id}_{normalized}" + internal[len(source):]
        raise ValueError("rota interna desconhecida")


STATE = TenantState(TENANT_ID) if TENANT_ID else None


class Handler(BaseHTTPRequestHandler):
    server_version = "cdnmnus-tenant-broker"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        assert STATE is not None
        route = self.path.split("?", 1)[0]
        if route == "/health":
            try:
                STATE.load(); status, body = HTTPStatus.OK, b"ok"
            except (OSError, ValueError, json.JSONDecodeError):
                status, body = HTTPStatus.SERVICE_UNAVAILABLE, b"unavailable"
            self.send_response(status); self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        action = self.headers.get("X-Broker-Action", "")
        if action not in ("resolve", "refresh", "resolve-vod", "refresh-vod"):
            self.send_error(HTTPStatus.NOT_FOUND); return
        try:
            STATE.validate_request(self.headers.get("X-CDN-Tenant", ""), self.headers.get("X-CDN-Public-Host", ""))
            vod = action.endswith("-vod")
            uri = legacy.safe_request_uri(self.headers.get("X-Original-URI", ""), vod=vod)
            internal = STATE.resolve(uri, force=action.startswith("refresh"), vod=vod)
            self.send_response(HTTPStatus.OK); self.send_header("X-Accel-Redirect", internal)
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", "0")
            self.end_headers()
        except PermissionError:
            self.send_error(HTTPStatus.BAD_GATEWAY, "destino bloqueado")
        except (OSError, ValueError, LookupError, json.JSONDecodeError):
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "origem indisponível")


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 512


def main() -> None:
    if STATE is None:
        raise SystemExit("CDNMNUS_TENANT_ID é obrigatório")
    STATE.load()
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    server = UnixHTTPServer(str(SOCKET_PATH), Handler)
    os.chmod(SOCKET_PATH, 0o660)
    try:
        server.serve_forever()
    finally:
        server.server_close(); SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
