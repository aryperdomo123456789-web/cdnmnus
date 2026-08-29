#!/usr/bin/env python3
"""Relay VOD privado por tenant.

Implementacao stdlib para canario: segue uma cadeia iniciada no XUI, exige que
o primeiro redirect aponte para uma seed administrada e conecta diretamente ao
IP validado. URLs e destinos nunca sao registrados nem devolvidos ao cliente.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import socketserver
import ssl
import sys
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

SNAPSHOT_PATH = Path(os.environ.get("CDNMNUS_TENANTS_CONFIG", "/opt/cdnmnus/current/broker/tenants.json"))
TENANT_ID = os.environ.get("CDNMNUS_TENANT_ID", "")
SOCKET_PATH = Path(os.environ.get("CDNMNUS_VOD_RELAY_SOCKET", f"/run/cdnmnus/vod-relay-{TENANT_ID}.sock"))
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RANGE_RE = re.compile(r"^bytes=(?:[0-9]+-[0-9]*|-[0-9]+)$")
REDIRECTS = frozenset((301, 302, 303, 307, 308))
PUBLIC_HEADERS = frozenset(("content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified"))
SINGLETON_RESPONSE_HEADERS = frozenset(("content-length", "content-range", "etag", "last-modified"))
MAX_URI = 4096
MAX_LOCATION = 8192
MAX_RESPONSE_HEADERS = 32768
BUFFER_SIZE = 64 * 1024


class BlockedDestination(Exception):
    pass


class InvalidRequest(Exception):
    pass


@dataclass(frozen=True)
class Seed:
    host: str
    schemes: frozenset[str]
    ports: frozenset[int]


@dataclass(frozen=True)
class Policy:
    origin_scheme: str
    origin_host: str
    origin_port: int
    seeds: tuple[Seed, ...]
    derived_ports: frozenset[int]
    max_redirects: int


def normalize_host(value: str) -> str:
    value = value.rstrip(".")
    if not value or len(value) > 253 or any(ord(ch) < 33 for ch in value):
        raise BlockedDestination()
    try:
        result = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise BlockedDestination() from exc
    if any(not label or len(label) > 63 for label in result.split(".")):
        raise BlockedDestination()
    return result


def default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def authority(target: SplitResult) -> str:
    host = normalize_host(target.hostname or "")
    port = target.port or default_port(target.scheme)
    return authority_from_parts(host, port, target.scheme)


def parse_target(raw: str) -> SplitResult:
    if len(raw) > MAX_LOCATION:
        raise BlockedDestination()
    target = urlsplit(raw)
    if target.scheme not in ("http", "https") or not target.hostname:
        raise BlockedDestination()
    if target.username is not None or target.password is not None or target.fragment:
        raise BlockedDestination()
    try:
        port = target.port or default_port(target.scheme)
    except ValueError as exc:
        raise BlockedDestination() from exc
    if port < 1 or port > 65535:
        raise BlockedDestination()
    normalize_host(target.hostname)
    return target


def validate_public_uri(raw: str) -> str:
    if not raw or len(raw) > MAX_URI or "\\" in raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise InvalidRequest()
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith(("/movie/", "/series/")):
        raise InvalidRequest()
    # Rejeita traversal literal e nas duas camadas comuns de percent encoding.
    probe = parsed.path.lower()
    for _ in range(2):
        probe = probe.replace("%2e", ".").replace("%25", "%")
        if any(part == ".." for part in probe.split("/")):
            raise InvalidRequest()
    return raw


def validate_range(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if len(value) > 128 or not RANGE_RE.fullmatch(value):
        raise InvalidRequest()
    return value


def resolve_public(host: str) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError("dns failure") from exc
    ips = tuple(sorted({item[4][0] for item in answers}))
    if not ips:
        raise OSError("empty dns answer")
    for raw in ips:
        ip = ipaddress.ip_address(raw)
        # is_global cobre loopback, RFC1918/ULA, link-local, multicast,
        # unspecified, documentation, CGNAT e ranges reservados.
        if not ip.is_global:
            raise BlockedDestination()
    return ips


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, port: int, pinned_ip: str, timeout: float = 10.0) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self.pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout, self.source_address)


class PinnedHTTPSConnection(PinnedHTTPConnection):
    def __init__(self, hostname: str, port: int, pinned_ip: str, timeout: float = 10.0) -> None:
        # http.client.HTTPConnection.__init__ calls self._get_hostport().  A
        # subclass named *HTTPSConnection therefore inherits DEFAULT_PORT=443
        # from the stdlib class through attribute lookup, but it does not
        # inherit HTTPSConnection's implementation.  Keep the requested port
        # explicit so the pinned dial cannot depend on that fragile detail.
        super().__init__(hostname, port, pinned_ip, timeout)
        self.pinned_port = port
        self.context = ssl.create_default_context()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.pinned_ip, self.pinned_port), self.timeout, self.source_address)
        assert self.sock is not None
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)


ConnectionFactory = Callable[[str, int, str, str], http.client.HTTPConnection]


def default_connection(scheme: str, host: str, port: int, ip: str) -> http.client.HTTPConnection:
    cls = PinnedHTTPSConnection if scheme == "https" else PinnedHTTPConnection
    return cls(host, port, ip)


def load_policy(snapshot_path: Path, tenant_id: str) -> Policy:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if int(snapshot.get("schema_version", 0)) not in (1, 2):
        raise ValueError("schema incompatível")
    tenant = snapshot.get("tenants", {}).get(tenant_id)
    if not isinstance(tenant, dict):
        raise ValueError("tenant ausente")
    origin = tenant.get("origin")
    if not isinstance(origin, dict):
        raise ValueError("origem ausente")
    policy = tenant.get("vod_policy", {})
    seed_rows = policy.get("seeds") if isinstance(policy, dict) else None
    if seed_rows is None:  # snapshot v1 compatível durante a migração
        seed_rows = [{"host": item["host"], "ports": [item.get("port", 80)],
                      "schemes": ["https" if int(item.get("port", 80)) == 443 else "http"]}
                     for item in tenant.get("vod_hosts", []) if isinstance(item, dict)]
    if not isinstance(seed_rows, list):
        raise ValueError("seeds VOD inválidas")
    seeds_list = []
    for item in seed_rows:
        if not isinstance(item, dict) or "host" not in item:
            raise ValueError("seed VOD inválida")
        schemes = frozenset(str(x) for x in item.get("schemes", ("http", "https")))
        ports = frozenset(int(x) for x in item.get("ports", (80, 443)))
        if not schemes or not schemes.issubset({"http", "https"}):
            raise ValueError("scheme de seed inválido")
        if not ports or any(port < 1 or port > 65535 for port in ports):
            raise ValueError("porta de seed inválida")
        seeds_list.append(Seed(normalize_host(str(item["host"])), schemes, ports))
    seeds = tuple(seeds_list)
    if not seeds:
        raise ValueError("nenhuma seed VOD")
    max_redirects = int(policy.get("max_redirects", 5)) if isinstance(policy, dict) else 5
    if not 1 <= max_redirects <= 5:
        raise ValueError("max_redirects inválido")
    scheme = str(origin.get("scheme", "https" if int(origin.get("port", 80)) == 443 else "http"))
    if scheme not in ("http", "https"):
        raise ValueError("scheme da origem inválido")
    origin_port = int(origin.get("port", default_port(scheme)))
    derived_ports = frozenset(int(x) for x in policy.get("derived_host_ports", (80, 443)))
    if not 1 <= origin_port <= 65535 or not derived_ports or any(port < 1 or port > 65535 for port in derived_ports):
        raise ValueError("porta VOD inválida")
    return Policy(scheme, normalize_host(str(origin["host"])), origin_port, seeds, derived_ports, max_redirects)


class Relay:
    def __init__(self, policy: Policy, resolver: Callable[[str], Iterable[str]] = resolve_public,
                 connection_factory: ConnectionFactory = default_connection) -> None:
        self.policy = policy
        self.resolver = resolver
        self.connection_factory = connection_factory

    def _allowed(self, target: SplitResult, first_vod_hop_seen: bool, is_origin: bool) -> None:
        host = normalize_host(target.hostname or "")
        port = target.port or default_port(target.scheme)
        if is_origin:
            if (target.scheme, host, port) != (self.policy.origin_scheme, self.policy.origin_host, self.policy.origin_port):
                raise BlockedDestination()
        elif not first_vod_hop_seen:
            if not any(host == seed.host and target.scheme in seed.schemes and port in seed.ports for seed in self.policy.seeds):
                raise BlockedDestination()
        elif port not in self.policy.derived_ports:
            raise BlockedDestination()

    def request(self, method: str, raw_uri: str, range_header: str | None, if_range: str | None):
        raw_uri = validate_public_uri(raw_uri)
        range_header = validate_range(range_header)
        if if_range is not None and (len(if_range) > 1024 or "\r" in if_range or "\n" in if_range):
            raise InvalidRequest()
        current = urlunsplit((self.policy.origin_scheme, authority_from_parts(self.policy.origin_host, self.policy.origin_port,
                                  self.policy.origin_scheme), raw_uri.split("?", 1)[0],
                                  raw_uri.split("?", 1)[1] if "?" in raw_uri else "", ""))
        first_vod_hop_seen = False
        for redirect_count in range(self.policy.max_redirects + 1):
            target = parse_target(current)
            self._allowed(target, first_vod_hop_seen, redirect_count == 0)
            ips = tuple(self.resolver(normalize_host(target.hostname or "")))
            if not ips:
                raise OSError("dns failure")
            # Revalida inclusive resolvers injetados; testes usam enderecos globais.
            if any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise BlockedDestination()
            port = target.port or default_port(target.scheme)
            conn = self.connection_factory(target.scheme, normalize_host(target.hostname or ""), port, ips[0])
            headers = {"Host": authority(target), "Accept-Encoding": "identity", "User-Agent": "cdnmnus-vod-relay/1"}
            if range_header: headers["Range"] = range_header
            if if_range: headers["If-Range"] = if_range
            path = target.path or "/"
            if target.query: path += "?" + target.query
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            if sum(len(k) + len(v) + 4 for k, v in response.getheaders()) > MAX_RESPONSE_HEADERS:
                conn.close(); raise OSError("oversized headers")
            if response.status in (200, 206):
                return conn, response
            if response.status not in REDIRECTS or redirect_count >= self.policy.max_redirects:
                response.read(8192); conn.close(); raise OSError("upstream failure")
            location = response.getheader("Location")
            response.read(8192); conn.close()
            if not location or len(location) > MAX_LOCATION:
                raise OSError("invalid redirect")
            next_target = parse_target(urljoin(current, location))
            self._allowed(next_target, first_vod_hop_seen, False)
            first_vod_hop_seen = True
            current = next_target.geturl()
        raise OSError("redirect limit")


def authority_from_parts(host: str, port: int, scheme: str) -> str:
    # RFC 3986 exige colchetes na autoridade de literais IPv6.
    rendered_host = f"[{host}]" if ":" in host else host
    return rendered_host if port == default_port(scheme) else f"{rendered_host}:{port}"


def public_response_headers(headers: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Seleciona headers seguros e rejeita framing ambíguo do upstream."""
    result = []
    seen: set[str] = set()
    for name, value in headers:
        lowered = name.lower()
        if lowered not in PUBLIC_HEADERS:
            continue
        if "\r" in value or "\n" in value or lowered in seen and lowered in SINGLETON_RESPONSE_HEADERS:
            raise OSError("invalid upstream headers")
        if lowered == "content-length" and (not value.isascii() or not value.isdigit()):
            raise OSError("invalid content length")
        seen.add(lowered)
        result.append((name, value))
    return tuple(result)


class Handler(BaseHTTPRequestHandler):
    server_version = "cdnmnus-vod-relay"
    relay: Relay

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        if self.path == "/health": self._health()
        else: self._handle()

    def do_GET(self) -> None:
        if self.path == "/health": self._health()
        else: self._handle()
    def do_POST(self) -> None: self._method_not_allowed()
    def do_CONNECT(self) -> None: self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED); self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0"); self.end_headers()

    def _health(self) -> None:
        body = b"ok\n"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)

    def _handle(self) -> None:
        conn = None
        try:
            conn, response = self.relay.request(self.command, self.path, self.headers.get("Range"), self.headers.get("If-Range"))
            # Valide integralmente antes de emitir a status line ao cliente;
            # assim um upstream malformado nunca produz duas respostas no socket.
            response_headers = public_response_headers(response.getheaders())
            self.send_response(response.status)
            for name, value in response_headers:
                self.send_header(name, value)
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = response.read(BUFFER_SIZE)
                    if not chunk: break
                    self.wfile.write(chunk)
        except InvalidRequest:
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid request")
        except BlockedDestination:
            self.send_error(HTTPStatus.BAD_GATEWAY, "upstream unavailable")
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "upstream unavailable")
        finally:
            if conn is not None:
                conn.close()


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 256


def main() -> None:
    if not TENANT_RE.fullmatch(TENANT_ID):
        raise SystemExit("CDNMNUS_TENANT_ID inválido")
    policy = load_policy(SNAPSHOT_PATH, TENANT_ID)
    Handler.relay = Relay(policy)
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
