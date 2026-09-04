#!/usr/bin/env python3
"""Broker por tenant sobre Unix socket, compatível com o resolver legado."""
from __future__ import annotations

import json
import http.client
import os
import re
import socketserver
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import token_broker as legacy
from playback.route_policy import normalize_channel_id, normalize_media_type
from playback.session_store import SessionStore
try:
    from core.playlist_tokens import PlaylistTokenStore
except ImportError:
    from playlist_tokens import PlaylistTokenStore
try:
    from core.m3u_transform import rewrite_public_playlist, sanitize_response_headers
except ImportError:
    from m3u_transform import rewrite_public_playlist, sanitize_response_headers

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
        self.playback_enabled = False
        self.playback_store = SessionStore(
            Path(os.environ.get("CDNMNUS_PLAYBACK_STORE", f"/run/cdnmnus/playback-{tenant_id}.db"))
        )
        self.playback_store.initialize()
        self.playlist_tokens = PlaylistTokenStore(
            os.environ.get("CDNMNUS_PLAYLIST_TOKEN_STORE", "/run/cdnmnus/playlist-tokens.db")
        )
        self.playlist_tokens.initialize()

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
                "origin_host_header": str(tenant.get("origin_host_header") or origin.get("host", "")),
                "live_redirect_passthrough": bool(tenant.get("live_redirect_passthrough", False)),
                "public_host": public_hosts[0],
                "load_balancers": [str(x["host"] if isinstance(x, dict) else x) for x in lbs],
                "playback_edges": [dict(x) for x in tenant.get("playback_edges", []) if isinstance(x, dict)],
                "vod_hosts": [str(x["host"] if isinstance(x, dict) else x) for x in vod],
                "ttl_seconds": int(tenant.get("ttl_seconds", 15)),
                "playback_sessions_v1": bool(int(tenant.get("playback_sessions_v1", 0))),
                "playlist_broker_enabled": bool(tenant.get("playlist_broker_enabled", False)),
            }
            if int(origin.get("port", 80)) != 80:
                raise ValueError("porta da origem ainda não suportada")
            self.hosts = set(public_hosts)
            self.state.config = config
            self.state.config_mtime = stat.st_mtime
            self.mtime = stat.st_mtime
            self.playback_enabled = bool(config.get("playback_sessions_v1", False))
        return self.state.config

    def validate_request(self, tenant_id: str, public_host: str) -> None:
        self.load()
        host = public_host.split(":", 1)[0].lower().rstrip(".")
        if tenant_id != self.tenant_id or host not in self.hosts:
            raise PermissionError("tenant/hostname divergente")

    def resolve(self, uri: str, force: bool, vod: bool) -> str:
        config = self.load()
        if uri.startswith("/play/pt1_"):
            token = urlsplit(uri).path.rsplit("/", 1)[-1]
            mapping = self.playlist_tokens.resolve(token, self.tenant_id)
            uri = legacy.safe_media_uri(str(mapping["internal_uri"]), vod=vod)
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
        if internal.startswith("/__cdnmnus_dynamic_vod/"):
            return f"/__cdnmnus_{self.tenant_id}_dynamic_vod/" + internal[len("/__cdnmnus_dynamic_vod/"):]
        raise ValueError("rota interna desconhecida")

    def _edge_candidates(self) -> list[dict[str, Any]]:
        config = self.load()
        enriched = config.get("playback_edges", [])
        if isinstance(enriched, list) and enriched:
            return [dict(item) for item in enriched if isinstance(item, dict)]
        hosts = [str(item).strip().lower() for item in config.get("load_balancers", []) if str(item).strip()]
        return [{"id": host, "host": host, "state": "ready", "last_health_status": 200, "weight": 100}
                for host in hosts]

    def create_playback_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.load()
        if not self.playback_enabled:
            raise FileNotFoundError("playback_sessions_v1 desativado")
        channel_id = normalize_channel_id(str(payload.get("channel_id", "")))
        media_type = normalize_media_type(str(payload.get("media_type", "live")))
        return self.playback_store.create_session(
            self.tenant_id,
            channel_id,
            media_type,
            self._edge_candidates(),
            ttl_seconds=int(self.state.config.get("ttl_seconds", 300)),
            media_uri=str(payload.get("media_uri") or f"/live/{channel_id}.ts"),
            public_host=str(self.state.config["public_host"]),
        )

    def record_playback_event(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.load()
        if not self.playback_enabled:
            raise FileNotFoundError("playback_sessions_v1 desativado")
        return self.playback_store.record_event(
            session_id, payload, self._edge_candidates(), public_host=str(self.state.config["public_host"])
        )

    def resolve_playback(self, session_id: str, media_type: str, query: str, public_host: str) -> str:
        """Resolve a signed session to the tenant-scoped internal upstream."""
        self.validate_request(self.tenant_id, public_host)
        values = parse_qs(query, keep_blank_values=False)
        token = str(values.get("token", [""])[0])
        channel_id = str(values.get("channel_id", [""])[0])
        if not token or not channel_id:
            raise PermissionError("playback sem token ou canal")
        session = self.playback_store.resolve_playback(
            session_id, token, tenant_id=self.tenant_id, channel_id=channel_id,
            media_type=media_type,
        )
        media_uri = str(session["media_uri"])
        if media_uri.startswith("/play/pt1_"):
            opaque = media_uri.split("/", 2)[-1].split("?", 1)[0]
            mapping = self.playlist_tokens.resolve(opaque, self.tenant_id)
            media_uri = legacy.safe_media_uri(str(mapping["internal_uri"]), vod=media_type == "vod")
        candidates = self._edge_candidates()
        for index, edge in enumerate(candidates):
            if str(edge.get("id") or edge.get("host")) == str(session["edge_id"]):
                return f"/__cdnmnus_{self.tenant_id}_lb_{index}{media_uri}"
        raise PermissionError("edge da sessão não pertence ao pool do tenant")

    def fetch_playlist(self, request_uri: str) -> tuple[int, dict[str, str], bytes]:
        config = self.load()
        origin = str(config["origin_host"])
        address = legacy.validated_addresses(origin)[0]
        # Large catalogues can arrive slowly and some XUI builds do not close
        # the attachment immediately after the final chunk. Keep this timeout
        # limited to playlist acquisition; live media keeps its own path.
        # Some XUI builds generate very large catalogues progressively. Keep
        # the longer wait isolated to playlist acquisition; media requests
        # continue using their existing short timeouts.
        body = b""
        headers: dict[str, str] = {}
        status = 0
        last_error: Exception | None = None
        for attempt in range(3):
            connection = http.client.HTTPConnection(address, 180)
            try:
                connection.request("GET", request_uri, headers={
                    "Host": str(config.get("origin_host_header") or config["public_host"]),
                    "User-Agent": "cdnmnus-playlist-broker/1.0",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                response = connection.getresponse()
                status = response.status
                headers = sanitize_response_headers(dict(response.getheaders()))
                body = response.read(128 * 1024 * 1024 + 1)
                if len(body) > 128 * 1024 * 1024:
                    raise ValueError("playlist acima do limite")
                if status in (200, 206):
                    break
                raise LookupError("playlist indisponível")
            except (OSError, LookupError) as exc:
                last_error = exc
                if attempt == 2:
                    raise
                time.sleep(1 << attempt)
            finally:
                connection.close()
        if status not in (200, 206):
            raise last_error or LookupError("playlist indisponível")
        try:
            content_type = headers.get("Content-Type", "application/vnd.apple.mpegurl")
            if content_type.lower().split(";", 1)[0].strip() == "application/octet-stream":
                content_type = "application/vnd.apple.mpegurl"
            transformed = rewrite_public_playlist(
                body,
                {"tenant_id": self.tenant_id, "canonical_host": str(config["public_host"]),
                 "origin_host": origin, "load_balancers": config.get("load_balancers", []),
                 "vod_hosts": config.get("vod_hosts", [])},
                max_bytes=128 * 1024 * 1024,
                # Brokered playlists must never expose the legacy
                # /username/password/... form, even when playback sessions
                # are disabled for the tenant.
                opaque_tokens=self.playback_enabled or bool(config.get("playlist_broker_enabled")),
                token_store=self.playlist_tokens,
                collect_urls=False,
            )
            output = transformed.body.encode("utf-8")
            return status, {"Content-Type": content_type}, output
        except Exception:
            raise


STATE = TenantState(TENANT_ID) if TENANT_ID else None


class Handler(BaseHTTPRequestHandler):
    server_version = "cdnmnus-tenant-broker"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        self.do_GET()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if route == "/get.php":
            try:
                status, headers, body = STATE.fetch_playlist(self.path)
                self._json_bytes(status, headers, body)
            except (OSError, ValueError, LookupError, UnicodeError, json.JSONDecodeError):
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "playlist indisponível")
            return
        match = re.fullmatch(r"/playback/(live|vod)/(ps-[a-f0-9]{32})", route)
        if match:
            try:
                internal = STATE.resolve_playback(
                    match.group(2), match.group(1), urlsplit(self.path).query,
                    self.headers.get("X-CDN-Public-Host", ""),
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("X-Accel-Redirect", internal)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except PermissionError:
                self.send_error(HTTPStatus.FORBIDDEN, "playback não autorizado")
            except (OSError, ValueError, LookupError):
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "playback indisponível")
            return
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

    def do_POST(self) -> None:
        assert STATE is not None
        route = self.path.split("?", 1)[0]
        try:
            if route == "/api/playback/sessions":
                payload = self._json_payload()
                self._json(HTTPStatus.CREATED, STATE.create_playback_session(payload))
                return
            match = re.fullmatch(r"/api/playback/sessions/([a-zA-Z0-9_-]+)/events", route)
            if match:
                payload = self._json_payload()
                self._json(HTTPStatus.OK, STATE.record_playback_event(match.group(1), payload))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except PermissionError:
            self.send_error(HTTPStatus.BAD_GATEWAY, "destino bloqueado")
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
        except (OSError, ValueError, LookupError, json.JSONDecodeError):
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "origem indisponível")

    def _json_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 64 * 1024:
            raise ValueError("corpo inválido")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json_bytes(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers.items():
            if name.lower() in {"content-type"}:
                self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


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
