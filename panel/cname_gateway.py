#!/usr/bin/env python3
"""Gateway DNS-only multi-tenant; o Host nunca vira um upstream."""
from __future__ import annotations

import http.client
import json
import os
import socket
import socketserver
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from core.cname_discovery import DiscoveryError, build_tenant_index, discover_alias, normalize_discovery_host, system_resolver
    from core.m3u_transform import rewrite_public_playlist, sanitize_response_headers
except ImportError:
    from cname_discovery import DiscoveryError, build_tenant_index, discover_alias, normalize_discovery_host, system_resolver
    from m3u_transform import rewrite_public_playlist, sanitize_response_headers
import token_broker

SNAPSHOT_PATH = Path(os.environ.get("CDNMNUS_TENANTS_CONFIG", "/opt/cdnmnus/current/broker/tenants.json"))
SOCKET_PATH = Path(os.environ.get("CDNMNUS_CNAME_GATEWAY_SOCKET", "/run/cdnmnus/cname-gateway.sock"))
MAX_REQUEST_URI = 16384
MAX_PLAYLIST_BYTES = int(os.environ.get("CDNMNUS_MAX_PLAYLIST_BYTES", str(128 * 1024 * 1024)))
MAX_API_BYTES = int(os.environ.get("CDNMNUS_MAX_API_BYTES", str(64 * 1024 * 1024)))
PUBLIC_HEADERS = {"content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified", "cache-control"}
ALLOWED_METHODS = {"GET", "HEAD"}


class GatewayError(Exception):
    pass


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 10.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def _clean_headers(headers: http.client.HTTPMessage | list[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    items = headers.items() if hasattr(headers, "items") else headers
    for name, value in items:
        lowered = name.lower()
        if lowered not in PUBLIC_HEADERS or lowered in seen:
            continue
        if "\r" in value or "\n" in value:
            raise GatewayError("header upstream inválido")
        if lowered == "content-length" and (not value.isascii() or not value.isdigit()):
            raise GatewayError("content-length inválido")
        seen.add(lowered)
        result.append((name, value))
    return result


def _read_limited(response: http.client.HTTPResponse, limit: int) -> bytes:
    body = bytearray()
    while len(body) <= limit:
        chunk = response.read(min(64 * 1024, limit + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
    raise GatewayError("resposta acima do limite")


class GatewayState:
    def __init__(self, resolver=None) -> None:
        self.resolver = resolver or system_resolver()
        self.mtime = -1.0
        self.snapshot: dict[str, object] = {}
        self.index = {}
        self.cache: dict[str, object] = {}
        self.lock = threading.RLock()

    def load(self) -> dict[str, object]:
        stat = SNAPSHOT_PATH.stat()
        with self.lock:
            if stat.st_mtime != self.mtime:
                snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
                if int(snapshot.get("schema_version", 0)) != 1:
                    raise GatewayError("snapshot incompatível")
                tenants = []
                for tenant_id, item in snapshot.get("tenants", {}).items():
                    if not isinstance(item, dict):
                        raise GatewayError("tenant inválido")
                    item = dict(item)
                    item["id"] = tenant_id
                    item["enabled"] = True
                    public_hosts = item.get("public_hosts", [])
                    if not isinstance(public_hosts, list) or not public_hosts:
                        raise GatewayError("tenant sem public_hosts")
                    item["canonical_host"] = str(public_hosts[0])
                    tenants.append(item)
                self.index = build_tenant_index(tenants)
                self.snapshot = snapshot
                self.cache.clear()
                self.mtime = stat.st_mtime
            return self.snapshot

    def decision(self, host: str):
        snapshot = self.load()
        normalized = normalize_discovery_host(host)
        with self.lock:
            cached = self.cache.get(normalized)
            if cached is not None and cached.expires_at > __import__("time").time():
                return cached
        result = discover_alias(normalized, self.index, self.resolver)
        with self.lock:
            self.cache[normalized] = result
        return result

    def tenant(self, tenant_id: str) -> dict[str, object]:
        item = self.snapshot.get("tenants", {}).get(tenant_id)
        if not isinstance(item, dict):
            raise GatewayError("tenant ausente")
        return item


STATE = GatewayState()


def _host_header(handler: BaseHTTPRequestHandler) -> str:
    value = handler.headers.get("Host", "").strip()
    if not value or ":" in value:
        raise DiscoveryError("Host com porta não permitido")
    return value


def _tenant_snapshot(tenant: dict[str, object], canonical: str) -> dict[str, object]:
    origin = tenant.get("origin")
    if not isinstance(origin, dict) or not origin.get("host"):
        raise GatewayError("origem do tenant ausente")
    return {
        "canonical_host": canonical,
        "origin_host": str(origin["host"]),
        "load_balancers": tenant.get("load_balancers", []),
        "vod_hosts": tenant.get("vod_hosts", []),
    }


def _origin_config(tenant: dict[str, object], canonical: str) -> dict[str, object]:
    origin = tenant.get("origin")
    lbs = tenant.get("load_balancers", [])
    if not isinstance(origin, dict):
        raise GatewayError("origem inválida")
    lb_hosts = [str(x.get("host" if isinstance(x, dict) else "", "")) for x in lbs]
    return {
        "origin_host": str(origin.get("host", "")),
        "public_host": canonical,
        "load_balancers": [x for x in lb_hosts if x],
        "ttl_seconds": 15,
    }


def _connect_host(host: str, port: int = 80, timeout: float = 10.0) -> tuple[http.client.HTTPConnection, str]:
    addresses = token_broker.validated_addresses(host)
    if not addresses:
        raise GatewayError("upstream sem endereço público")
    address = addresses[0]
    return http.client.HTTPConnection(address, port, timeout=timeout), address


def _request_headers(handler: BaseHTTPRequestHandler, canonical: str) -> dict[str, str]:
    headers = {"Host": canonical, "User-Agent": handler.headers.get("User-Agent", "cdnmnus-cname-gateway/1")[:512],
               "Accept-Encoding": "identity", "Connection": "close"}
    for name in ("Accept", "Range", "If-Range"):
        value = handler.headers.get(name)
        if value is not None:
            headers[name] = value[:4096]
    return headers


def _proxy_response(handler: BaseHTTPRequestHandler, response: http.client.HTTPResponse, body: bytes | None = None) -> None:
    headers = _clean_headers(response.getheaders())
    handler.send_response(response.status)
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    if handler.command != "HEAD":
        if body is not None:
            handler.wfile.write(body)
        else:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)


def _api(handler: BaseHTTPRequestHandler, tenant: dict[str, object], canonical: str) -> None:
    origin = tenant["origin"]
    assert isinstance(origin, dict)
    port = int(origin.get("port", 80))
    connection, _ = _connect_host(str(origin["host"]), port)
    try:
        connection.request(handler.command, handler.path, headers=_request_headers(handler, canonical))
        response = connection.getresponse()
        if handler.path.split("?", 1)[0] == "/get.php":
            body = _read_limited(response, MAX_PLAYLIST_BYTES)
            text = body.decode("utf-8", "strict")
            transformed = rewrite_public_playlist(text, _tenant_snapshot(tenant, canonical), max_bytes=MAX_PLAYLIST_BYTES)
            safe = sanitize_response_headers(dict(response.getheaders()))
            handler.send_response(response.status)
            for name, value in safe.items():
                if name.lower() in PUBLIC_HEADERS and name.lower() != "content-length":
                    handler.send_header(name, value)
            handler.send_header("Content-Length", str(len(transformed.body.encode("utf-8"))))
            handler.end_headers()
            if handler.command != "HEAD":
                handler.wfile.write(transformed.body.encode("utf-8"))
        else:
            # Xtream clients request the complete VOD catalog here. Keep a
            # bounded limit, but allow catalogs larger than the live API.
            body = _read_limited(response, MAX_API_BYTES)
            _proxy_response(handler, response, body)
    finally:
        connection.close()


def _live(handler: BaseHTTPRequestHandler, tenant: dict[str, object], canonical: str) -> None:
    config = _origin_config(tenant, canonical)
    internal = token_broker.query_origin(handler.path, config)
    if internal.startswith("/__cdnmnus_resolved_origin"):
        host = str(config["origin_host"]); path = internal[len("/__cdnmnus_resolved_origin"):]; port = 80
    elif internal.startswith("/__cdnmnus_resolved_lb_"):
        index_end = internal.find("/", len("/__cdnmnus_resolved_lb_"))
        index = int(internal[len("/__cdnmnus_resolved_lb_"):index_end])
        host = str(config["load_balancers"][index]); path = internal[index_end:]; port = 80
    else:
        raise GatewayError("resolução interna desconhecida")
    connection, _ = _connect_host(host, port)
    try:
        connection.request(handler.command, path, headers=_request_headers(handler, canonical))
        _proxy_response(handler, connection.getresponse())
    finally:
        connection.close()


def _vod(handler: BaseHTTPRequestHandler, tenant_id: str) -> None:
    connection = UnixHTTPConnection(f"/run/cdnmnus/vod-relay-{tenant_id}.sock")
    headers = {"Host": "localhost", "Connection": "close"}
    for name in ("Range", "If-Range", "User-Agent"):
        value = handler.headers.get(name)
        if value is not None:
            headers[name] = value[:4096]
    try:
        connection.request(handler.command, handler.path, headers=headers)
        _proxy_response(handler, connection.getresponse())
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "cdnmnus-cname-gateway"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    def _handle(self) -> None:
        if len(self.path) > MAX_REQUEST_URI:
            self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
            return
        route = urlsplit(self.path).path
        if route in ("/", "/edge-health") or route.startswith(("/admin", "/administrator", "/phpmyadmin", "/internal")):
            self.send_error(HTTPStatus.MISDIRECTED_REQUEST, "route blocked")
            return
        try:
            decision = STATE.decision(_host_header(self))
            tenant = STATE.tenant(decision.tenant_id or "")
            canonical = decision.canonical_host or ""
            if route in ("/get.php", "/player_api.php"):
                _api(self, tenant, canonical)
            elif route.startswith(("/movie/", "/series/")):
                _vod(self, decision.tenant_id or "")
            elif route.startswith(("/live/", "/hls/")) or __import__("re").fullmatch(r"/[^/]+/[^/]+/[0-9]+\.m3u8", route):
                _live(self, tenant, canonical)
            else:
                self.send_error(HTTPStatus.MISDIRECTED_REQUEST, "route blocked")
        except DiscoveryError:
            self.send_error(HTTPStatus.MISDIRECTED_REQUEST, "tenant not discovered")
        except (GatewayError, OSError, ValueError, LookupError, http.client.HTTPException):
            self.send_error(HTTPStatus.BAD_GATEWAY, "upstream unavailable")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 256


def main() -> None:
    STATE.load()
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    server = Server(str(SOCKET_PATH), Handler)
    os.chmod(SOCKET_PATH, 0o660)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        SOCKET_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
