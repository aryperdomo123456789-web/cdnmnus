#!/usr/bin/env python3
"""Resolve redirects HLS autorizados sem expor tokens ou origens ao cliente."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_PATH = Path(os.environ.get("CDNMNUS_TOKEN_CONFIG", "/etc/cdnmnus/token-broker.json"))
BIND = os.environ.get("CDNMNUS_TOKEN_BIND", "127.0.0.1")
PORT = int(os.environ.get("CDNMNUS_TOKEN_PORT", "9091"))
MAX_URI = 4096
MAX_LOCATION = 8192


@dataclass
class Entry:
    internal_uri: str
    expires_at: float


class BrokerState:
    def __init__(self) -> None:
        self.cache: dict[str, Entry] = {}
        self.locks: dict[str, threading.Lock] = {}
        self.guard = threading.Lock()
        self.config_mtime = -1.0
        self.config: dict[str, object] = {}
        self.operations = 0

    def load_config(self) -> dict[str, object]:
        stat = CONFIG_PATH.stat()
        if stat.st_mtime != self.config_mtime:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            origin = str(data.get("origin_host", ""))
            public = str(data.get("public_host", ""))
            lbs = [str(x) for x in data.get("load_balancers", [])]
            if not origin or not public:
                raise ValueError("configuração incompleta")
            data["origin_host"] = origin
            data["public_host"] = public
            data["load_balancers"] = lbs
            data["ttl_seconds"] = max(2, min(30, int(data.get("ttl_seconds", 15))))
            self.config, self.config_mtime = data, stat.st_mtime
        return self.config

    def lock_for(self, key: str) -> threading.Lock:
        with self.guard:
            return self.locks.setdefault(key, threading.Lock())

    def resolve(self, uri: str, force: bool = False, vod: bool = False) -> str:
        self.operations += 1
        if self.operations % 100 == 0:
            self.prune()
        key = hashlib.sha256(uri.encode()).hexdigest()
        now = time.monotonic()
        if not force and (entry := self.cache.get(key)) and entry.expires_at > now:
            return entry.internal_uri
        with self.lock_for(key):
            now = time.monotonic()
            if not force and (entry := self.cache.get(key)) and entry.expires_at > now:
                return entry.internal_uri
            config = self.load_config()
            internal = query_vod(uri, config) if vod else query_origin(uri, config)
            self.cache[key] = Entry(internal, now + int(config["ttl_seconds"]))
            return internal

    def prune(self) -> None:
        now = time.monotonic()
        with self.guard:
            expired = [key for key, entry in self.cache.items() if entry.expires_at <= now]
            for key in expired:
                self.cache.pop(key, None)
            for key in list(self.locks):
                if key not in self.cache and not self.locks[key].locked():
                    self.locks.pop(key, None)
            if len(self.cache) > 100_000:
                oldest = sorted(self.cache, key=lambda key: self.cache[key].expires_at)[:len(self.cache) - 100_000]
                for key in oldest:
                    self.cache.pop(key, None)


STATE = BrokerState()


def safe_request_uri(raw: str, vod: bool = False) -> str:
    path = urlsplit(raw).path if raw else ""
    credential_manifest = bool(re.fullmatch(r"/[^/?]+/[^/?]+/[0-9]+\.m3u8", path))
    allowed = raw.startswith(("/movie/", "/series/")) if vod else (raw.startswith(("/hls/", "/live/")) or credential_manifest)
    if not raw or len(raw) > MAX_URI or not allowed:
        raise ValueError("rota de mídia inválida")
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.fragment or ".." in parsed.path.split("/"):
        raise ValueError("URI inválida")
    return parsed.path + (("?" + parsed.query) if parsed.query else "")


def host_addresses(host: str) -> list[str]:
    return sorted({item[4][0] for item in socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)})


def validated_addresses(host: str) -> list[str]:
    addresses = host_addresses(host)
    if not addresses:
        raise ValueError("destino sem endereço TCP")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("destino resolve endereço não permitido")
    return addresses


def validate_configured_host(host: str) -> None:
    validated_addresses(host)


def query_origin(uri: str, config: dict[str, object]) -> str:
    origin = str(config["origin_host"]).lower()
    public_host = str(config["public_host"])
    load_balancers = [str(x).lower() for x in config.get("load_balancers", [])]
    allowed_hosts = {origin, public_host.lower(), *load_balancers}
    host, path = origin, uri
    redirect_statuses = (HTTPStatus.MOVED_PERMANENTLY, HTTPStatus.FOUND,
                         HTTPStatus.TEMPORARY_REDIRECT, HTTPStatus.PERMANENT_REDIRECT)
    for hop in range(5):
        # Every hop is pinned after DNS validation. A redirect can never add a
        # new destination; it must already belong to this tenant's allowlist.
        if host not in allowed_hosts:
            raise PermissionError("redirect fora da allowlist")
        address = validated_addresses(host)[0]
        connection = http.client.HTTPConnection(address, 80, timeout=5)
        try:
            connection.request("GET", path, headers={
                "Host": public_host if hop == 0 else host,
                "User-Agent": "cdnmnus-token-broker/1.0",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            response = connection.getresponse()
            location = response.getheader("Location", "")
            response.read(4096)
        finally:
            connection.close()
        if response.status not in redirect_statuses:
            if response.status not in (HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT):
                raise LookupError(f"upstream_status_{response.status}")
            if host in load_balancers:
                return f"/__cdnmnus_resolved_lb_{load_balancers.index(host)}{path}"
            if host == origin or host == public_host.lower():
                return "/__cdnmnus_resolved_origin" + path
            raise PermissionError("destino final fora da allowlist")
        if not location or len(location) > MAX_LOCATION:
            raise ValueError("redirect ausente ou excessivo")
        parsed = urlsplit(location)
        if parsed.username or parsed.password or parsed.fragment or parsed.scheme not in ("", "http"):
            raise ValueError("redirect inválido")
        target_host = (parsed.hostname or host).lower()
        target_port = parsed.port or 80
        if target_port != 80:
            raise ValueError("porta de redirect não autorizada")
        next_path = parsed.path or "/"
        if not next_path.startswith("/") or ".." in next_path.split("/"):
            raise ValueError("caminho de redirect inválido")
        if parsed.query:
            next_path += "?" + parsed.query
        if target_host not in allowed_hosts:
            raise PermissionError("redirect fora da allowlist")
        # Preserve the legacy contract for providers that repeat the same LB
        # redirect: the internal LB route is already the safe terminal hop.
        if target_host == host and target_host in load_balancers:
            return f"/__cdnmnus_resolved_lb_{load_balancers.index(target_host)}{next_path}"
        host, path = target_host, next_path
    raise LookupError("excesso de redirects")


def query_vod(uri: str, config: dict[str, object]) -> str:
    """Segue somente a cadeia VOD autorizada e nunca devolve Location público."""
    origin = str(config["origin_host"]).lower()
    public_host = str(config["public_host"])
    vod_hosts = [str(x).lower() for x in config.get("vod_hosts", [])]
    allowed = [origin, *vod_hosts]
    host, path = origin, uri
    for hop in range(5):
        # O primeiro salto precisa ser um fornecedor cadastrado. Saltos
        # seguintes podem mudar de hostname como no navegador, mas continuam
        # sujeitos a DNS público e ao limite de redirects.
        if hop == 0 and host not in allowed:
            raise PermissionError("destino VOD fora da allowlist")
        address = validated_addresses(host)[0]
        connection = http.client.HTTPConnection(address, 80, timeout=8)
        try:
            connection.request("GET", path, headers={
                "Host": public_host if hop == 0 else host,
                "User-Agent": "cdnmnus-token-broker/1.0",
                "Accept-Encoding": "identity",
                "Range": "bytes=0-0",
                "Connection": "close",
            })
            response = connection.getresponse()
            location = response.getheader("Location", "")
            response.read(1)
        finally:
            connection.close()
        if response.status in (HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT):
            if host == origin:
                return "/__cdnmnus_resolved_origin" + path
            if host in vod_hosts:
                return f"/__cdnmnus_vod_{vod_hosts.index(host)}" + path
            # Host final descoberto pelo redirect, equivalente ao navegador.
            # O prefixo é internal no Nginx; o cliente nunca pode fornecê-lo.
            # X-Accel-Redirect passa novamente pelo parser de URI do Nginx.
            # Preserve escapes percentuais do fornecedor por duas camadas;
            # sem isso, caminhos assinados com %xx são decodificados antes do
            # proxy e o storage final responde 400 por assinatura divergente.
            internal_path = path.replace("%", "%25")
            return f"/__cdnmnus_dynamic_vod/{host}{internal_path}"
        if response.status not in (HTTPStatus.MOVED_PERMANENTLY, HTTPStatus.FOUND, HTTPStatus.TEMPORARY_REDIRECT, HTTPStatus.PERMANENT_REDIRECT):
            raise LookupError(f"vod_status_{response.status}")
        parsed = urlsplit(location)
        if not location or parsed.username or parsed.password or parsed.fragment or parsed.scheme not in ("", "http"):
            raise ValueError("redirect VOD inválido")
        host = (parsed.hostname or host).lower()
        if (parsed.port or 80) != 80:
            raise ValueError("porta VOD não autorizada")
        path = parsed.path or "/"
        if ".." in path.split("/"):
            raise ValueError("caminho VOD inválido")
        if parsed.query:
            path += "?" + parsed.query
    raise LookupError("excesso de redirects VOD")


class Handler(BaseHTTPRequestHandler):
    server_version = "cdnmnus-token-broker"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        route = self.path.split("?", 1)[0]
        if route == "/health":
            try:
                STATE.load_config()
                status, body = HTTPStatus.OK, b"ok"
            except (OSError, ValueError, json.JSONDecodeError):
                status, body = HTTPStatus.SERVICE_UNAVAILABLE, b"unavailable"
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        action = self.headers.get("X-Broker-Action", "")
        if action not in ("resolve", "refresh", "resolve-vod", "refresh-vod"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            vod = action.endswith("-vod")
            uri = safe_request_uri(self.headers.get("X-Original-URI", ""), vod=vod)
            internal = STATE.resolve(uri, force=action.startswith("refresh"), vod=vod)
            if action.startswith("refresh"):
                if vod and internal.startswith("/__cdnmnus_vod_"):
                    internal = internal.replace("/__cdnmnus_vod_", "/__cdnmnus_vod_retry_", 1)
                else:
                    internal = internal.replace("/__cdnmnus_resolved_", "/__cdnmnus_retry_", 1)
            self.send_response(HTTPStatus.OK)
            self.send_header("X-Accel-Redirect", internal)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except PermissionError:
            self.send_error(HTTPStatus.BAD_GATEWAY, "destino bloqueado")
        except (OSError, ValueError, LookupError, json.JSONDecodeError):
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "origem indisponível")


class BrokerServer(ThreadingHTTPServer):
    request_queue_size = 512


def main() -> None:
    STATE.load_config()
    server = BrokerServer((BIND, PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
