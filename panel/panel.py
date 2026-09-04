#!/usr/bin/env python3
"""cdnmnus: painel local/autenticado para um reverse proxy HTTP autorizado."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ipaddress
import re
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import grp
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BIND = os.environ.get("CDNMNUS_PANEL_BIND", "127.0.0.1")
PORT = int(os.environ.get("CDNMNUS_PANEL_PORT", "9090"))
DB_PATH = Path(os.environ.get("CDNMNUS_PANEL_DB", "/etc/cdnmnus/panel.db"))
NGINX_INCLUDE = Path(os.environ.get("CDNMNUS_NGINX_INCLUDE", "/etc/nginx/conf.d/99-cdnmnus-upstream.conf"))
TOKEN_BROKER_CONFIG = Path(os.environ.get("CDNMNUS_TOKEN_CONFIG", "/etc/cdnmnus/token-broker.json"))
PUBLIC_HOST = os.environ.get("CDNMNUS_PUBLIC_HOST", "")
PANEL_USER = os.environ.get("CDNMNUS_PANEL_USER", "admin")
PANEL_PASSWORD = os.environ.get("CDNMNUS_PANEL_PASSWORD", "")
VOD_SEED_HOSTS = os.environ.get("CDNMNUS_VOD_SEED_HOSTS", "servicedovod.lat")
MAX_BODY = 16 * 1024
PBKDF2_ITERATIONS = 310_000
HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.*_-]+$")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def valid_host(value: str) -> bool:
    if not value or len(value) > 253 or not HOST_RE.fullmatch(value):
        return False
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return bool(re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", value))


def resolve_host(host: str) -> list[str]:
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    if not addresses:
        raise ValueError("DNS não resolveu nenhum endereço TCP")
    return addresses


def normalize_host_input(raw: str, label: str, require_http: bool = False) -> str:
    value = raw.strip()
    if not value:
        raise ValueError(f"{label} é obrigatório")
    candidate = value if "://" in value else f"http://{value}"
    parsed = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(candidate)
    if require_http and parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"{label} deve usar HTTP ou HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{label} não pode conter credenciais, query string ou fragmento")
    if parsed.path not in ("", "/"):
        raise ValueError(f"{label} não pode conter caminho")
    hostname = parsed.hostname or ""
    if not valid_host(hostname):
        raise ValueError(f"{label} deve ser um IP ou DNS válido")
    return hostname


def normalize_config(payload: dict[str, Any]) -> dict[str, Any]:
    raw_upstream = str(payload.get("upstream_host", ""))
    upstream_candidate = raw_upstream if "://" in raw_upstream else f"http://{raw_upstream}"
    parsed_upstream = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(upstream_candidate)
    if parsed_upstream.scheme.lower() != "http":
        raise ValueError("o upstream do XUI deve usar HTTP")
    host = normalize_host_input(raw_upstream, "upstream_host", require_http=True)
    port = parsed_upstream.port or payload.get("upstream_port", 80)
    public_host = normalize_host_input(str(payload.get("public_host", PUBLIC_HOST)), "public_host", require_http=True)
    if port != 80:
        raise ValueError("o upstream deve usar HTTP na porta 80 neste perfil")
    addresses = resolve_host(host)
    raw_lbs = payload.get("load_balancers", "")
    if isinstance(raw_lbs, str):
        lb_values = [item.strip() for item in raw_lbs.replace(",", "\n").splitlines() if item.strip()]
    elif isinstance(raw_lbs, list):
        lb_values = [str(item).strip() for item in raw_lbs if str(item).strip()]
    else:
        raise ValueError("load_balancers deve ser uma lista de IPs ou DNS")
    load_balancers: list[str] = []
    for index, value in enumerate(lb_values, 1):
        candidate = value if "://" in value else f"http://{value}"
        parsed = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(candidate)
        if parsed.scheme.lower() != "http" or (parsed.port or 80) != 80:
            raise ValueError(f"load balancer {index} deve usar HTTP na porta 80")
        lb_host = normalize_host_input(value, f"load balancer {index}", require_http=True)
        resolve_host(lb_host)
        if lb_host != host and lb_host not in load_balancers:
            load_balancers.append(lb_host)
    return {
        "scheme": "http",
        "upstream_host": host,
        "upstream_port": 80,
        "resolved_addresses": addresses,
        "public_host": public_host or "_",
        "load_balancers": load_balancers,
    }


def nginx_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def configured_vod_hosts() -> list[str]:
    """Return the explicit VOD seeds, rejecting unsafe/ambiguous host values."""
    hosts: list[str] = []
    for position, raw_host in enumerate(VOD_SEED_HOSTS.split(","), 1):
        host = raw_host.strip().lower()
        if not host:
            continue
        if not valid_host(host):
            raise ValueError(f"CDNMNUS_VOD_SEED_HOSTS contém host inválido na posição {position}")
        if host not in hosts:
            hosts.append(host)
    if not hosts:
        raise ValueError("CDNMNUS_VOD_SEED_HOSTS deve conter ao menos uma origem VOD")
    return hosts


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")


def verify_password(password: str, salt_b64: str, digest_b64: str) -> bool:
    try:
        salt = base64.b64decode(salt_b64.encode("ascii"), validate=True)
        expected = base64.b64decode(digest_b64.encode("ascii"), validate=True)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


def initialize_db() -> None:
    with db_connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                must_change INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        user = db.execute("SELECT username FROM users LIMIT 1").fetchone()
        if user is None:
            if not PANEL_PASSWORD:
                raise RuntimeError("CDNMNUS_PANEL_PASSWORD não configurada para o primeiro bootstrap")
            salt, digest = hash_password(PANEL_PASSWORD)
            db.execute("INSERT INTO users(username,salt,password_hash,must_change) VALUES(?,?,?,1)", (PANEL_USER, salt, digest))
        elif os.environ.get("CDNMNUS_PANEL_FORCE_RESET") == "1" and PANEL_PASSWORD:
            salt, digest = hash_password(PANEL_PASSWORD)
            db.execute("UPDATE users SET username=?,salt=?,password_hash=?,must_change=1,updated_at=CURRENT_TIMESTAMP", (PANEL_USER, salt, digest))
        legacy = Path("/etc/cdnmnus/upstream.json")
        if not db.execute("SELECT 1 FROM settings LIMIT 1").fetchone() and legacy.exists():
            try:
                data = json.loads(legacy.read_text(encoding="utf-8"))
                for key, value in data.items():
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))
                legacy.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                pass
    os.chmod(DB_PATH, 0o600)


def get_user(username: str) -> sqlite3.Row | None:
    with db_connect() as db:
        return db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def authenticated_user(handler: BaseHTTPRequestHandler) -> sqlite3.Row | None:
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    user = get_user(username)
    if user is None or not verify_password(password, user["salt"], user["password_hash"]):
        return None
    return user


def config_from_db() -> dict[str, Any]:
    with db_connect() as db:
        rows = db.execute("SELECT key,value FROM settings").fetchall()
    result: dict[str, Any] = {}
    for row in rows:
        try:
            result[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            result[row["key"]] = row["value"]
    return result


def save_config(config: dict[str, Any]) -> None:
    with db_connect() as db:
        for key, value in config.items():
            db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)", (key, json.dumps(value, ensure_ascii=False)))
    os.chmod(DB_PATH, 0o600)


def replace_config(config: dict[str, Any]) -> None:
    """Substitui settings integralmente para permitir rollback exato."""
    with db_connect() as db:
        db.execute("DELETE FROM settings")
        db.executemany(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)",
            ((key, json.dumps(value, ensure_ascii=False)) for key, value in config.items()),
        )
    os.chmod(DB_PATH, 0o600)


def profile_view(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config.get(key, default) for key, default in (
        ("profile_id", ""), ("name", config.get("upstream_host", "")),
        ("upstream_host", ""), ("upstream_port", 80), ("public_host", ""),
        ("load_balancers", []),
    )}


def stored_profiles(config: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = config.get("profiles")
    if isinstance(profiles, list) and profiles:
        return [profile_view(item) for item in profiles if isinstance(item, dict)]
    legacy = profile_view(config)
    legacy["profile_id"] = legacy["profile_id"] or secrets.token_hex(8)
    return [legacy] if legacy.get("upstream_host") else []


def save_profile(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_config(payload)
    config = config_from_db()
    profiles = stored_profiles(config)
    profile_id = str(payload.get("profile_id", "")).strip() or secrets.token_hex(8)
    profile = profile_view({**normalized, "profile_id": profile_id, "name": str(payload.get("name", "")).strip() or normalized["upstream_host"]})
    profiles = [item for item in profiles if item["profile_id"] != profile_id]
    profiles.append(profile)
    normalized.update({"profile_id": profile_id, "name": profile["name"], "profiles": profiles, "active_profile_id": profile_id})
    apply_config(normalized)
    return normalized


def delete_profile(profile_id: str) -> dict[str, Any]:
    config = config_from_db()
    profiles = [item for item in stored_profiles(config) if item["profile_id"] != profile_id]
    if len(profiles) == len(stored_profiles(config)):
        raise ValueError("perfil não encontrado")
    if not profiles:
        raise ValueError("mantenha pelo menos um perfil XUI")
    active_id = config.get("active_profile_id")
    if active_id == profile_id or not active_id:
        target = dict(profiles[0])
    else:
        target = dict(next(item for item in profiles if item["profile_id"] == active_id))
    target["profiles"] = profiles
    target["active_profile_id"] = target["profile_id"]
    normalized = normalize_config(target)
    normalized.update({"profile_id": target["profile_id"], "name": target["name"], "profiles": profiles, "active_profile_id": target["profile_id"]})
    apply_config(normalized)
    return normalized


def profile_status(profile: dict[str, Any], active_id: str) -> str:
    try:
        addresses = resolve_host(str(profile["upstream_host"]))
        online = False
        for address in addresses:
            connection = socket.create_connection((address, 80), timeout=1)
            connection.close()
            online = True
            break
    except (OSError, ValueError):
        online = False
    if profile.get("profile_id") == active_id:
        return "Ativo" if online else "Inativo"
    return "Online" if online else "Inativo"


def api_profiles(config: dict[str, Any]) -> dict[str, Any]:
    profiles = stored_profiles(config)
    active_id = str(config.get("active_profile_id", ""))
    result = []
    for profile in profiles:
        item = profile_view(profile)
        item["status"] = profile_status(item, active_id)
        item["load_balancer_count"] = len(item["load_balancers"])
        result.append(item)
    return {"profiles": result, "active_profile_id": active_id}


def render_include(config: dict[str, Any]) -> str:
    host = str(config["upstream_host"])
    server_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    upstream_name = "cdnmnus_dynamic_backend"
    load_balancers = [str(item) for item in config.get("load_balancers", []) if valid_host(str(item))]
    candidates = [host, *config.get("resolved_addresses", []), *load_balancers]
    filters: list[str] = []
    redirects: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not valid_host(candidate):
            continue
        url_host = f"[{candidate}]" if ":" in candidate and not candidate.startswith("[") else candidate
        for prefix, replacement_prefix in (("http://", "https://"), ("https://", "https://"), ("//", "//")):
            for suffix in (":80", ""):
                source = f"{prefix}{url_host}{suffix}"
                replacement = f"{replacement_prefix}$host"
                line = f'    sub_filter "{nginx_escape(source)}" "{nginx_escape(replacement)}";'
                if line not in seen:
                    seen.add(line)
                    filters.append(line)
                bare_line = f'    sub_filter "{nginx_escape(url_host)}" "$host";'
                if bare_line not in seen:
                    seen.add(bare_line)
                    filters.append(bare_line)
                target = "/__cdnmnus_lb__/" if candidate in load_balancers else "https://$host/"
                redirect = f'    proxy_redirect "{nginx_escape(source)}/" "{target}";'
                if redirect not in redirects:
                    redirects.append(redirect)
    cert_name = str(config.get("public_host", ""))
    tls_config = "    listen 443 ssl;\n    ssl_certificate /etc/letsencrypt/live/%s/fullchain.pem;\n    ssl_certificate_key /etc/letsencrypt/live/%s/privkey.pem;\n    ssl_protocols TLSv1.2 TLSv1.3;\n    ssl_session_cache shared:cdnmnus_tls:10m;\n    ssl_session_timeout 1d;\n" % (nginx_escape(cert_name), nginx_escape(cert_name))
    lb_servers = "\n".join(f"    server {nginx_escape(item)}:80 max_fails=2 fail_timeout=5s;" for item in load_balancers)
    lb_upstream = f"upstream cdnmnus_load_balancers {{\n{lb_servers}\n    keepalive 32;\n}}\n\n" if load_balancers else ""
    resolved_upstreams = "\n".join(
        f"upstream cdnmnus_resolved_lb_{index} {{\n    server {nginx_escape(item)}:80 max_fails=2 fail_timeout=5s;\n    keepalive 32;\n}}"
        for index, item in enumerate(load_balancers)
    )
    resolved_locations = "\n".join(f'''    location ^~ /__cdnmnus_resolved_lb_{index}/ {{
        internal;
        proxy_pass http://cdnmnus_resolved_lb_{index}/;
        proxy_buffering on;
        proxy_cache cdnmnus_hls;
        proxy_cache_bypass $cdnmnus_skip_hls_cache;
        proxy_no_cache $cdnmnus_skip_hls_cache;
        proxy_cache_key "$scheme|$request_method|$host|$uri";
        proxy_cache_bypass $http_range;
        proxy_no_cache $http_range;
        proxy_cache_valid 200 6s;
        proxy_cache_lock on;
        proxy_cache_lock_timeout 2s;
        proxy_cache_lock_age 5s;
        proxy_read_timeout 20s;
        proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
        proxy_intercept_errors on;
        error_page 401 403 404 410 500 502 503 504 = @cdnmnus_refresh;
        proxy_hide_header Server;
        proxy_hide_header Location;
    }}

    location ^~ /__cdnmnus_retry_lb_{index}/ {{
        internal;
        proxy_pass http://cdnmnus_resolved_lb_{index}/;
        proxy_buffering on;
        proxy_cache cdnmnus_hls;
        proxy_cache_bypass $cdnmnus_skip_hls_cache;
        proxy_no_cache $cdnmnus_skip_hls_cache;
        proxy_cache_key "$scheme|$request_method|$host|$uri";
        proxy_cache_bypass $http_range;
        proxy_no_cache $http_range;
        proxy_cache_valid 200 6s;
        proxy_cache_lock on;
        proxy_cache_lock_timeout 2s;
        proxy_cache_lock_age 5s;
        proxy_read_timeout 20s;
        proxy_hide_header Server;
        proxy_hide_header Location;
    }}''' for index, _ in enumerate(load_balancers))
    vod_hosts = configured_vod_hosts()
    vod_upstreams = "\n\n".join(
        f"upstream cdnmnus_vod_{index} {{\n"
        f"    server {nginx_escape(vod_host)}:80 max_fails=2 fail_timeout=5s;\n"
        "    keepalive 32;\n}"
        for index, vod_host in enumerate(vod_hosts)
    ) + "\n\n"
    vod_locations = "\n\n".join(f'''    location ^~ /__cdnmnus_vod_{index}/ {{
        internal;
        proxy_pass http://cdnmnus_vod_{index}/;
        proxy_set_header Host {nginx_escape(vod_host)};
        proxy_cache cdnmnus_hls;
        proxy_cache_methods GET HEAD;
        proxy_cache_key "$host|vod{index}|$uri";
        proxy_cache_bypass $http_range;
        proxy_no_cache $http_range;
        proxy_cache_valid 200 30s;
        proxy_cache_lock on;
        proxy_cache_lock_timeout 2s;
        proxy_cache_lock_age 5s;
        proxy_intercept_errors on;
        error_page 401 403 404 410 500 502 503 504 = @cdnmnus_refresh;
        proxy_hide_header Server;
        proxy_hide_header Location;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }}

    location ^~ /__cdnmnus_vod_retry_{index}/ {{
        internal;
        proxy_pass http://cdnmnus_vod_{index}/;
        proxy_set_header Host {nginx_escape(vod_host)};
        proxy_hide_header Server;
        proxy_hide_header Location;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }}''' for index, vod_host in enumerate(vod_hosts))
    dynamic_vod_location = '''    # Redirect VOD final validado pelo broker; rota interna, nunca acessível
    # diretamente pelo cliente.
    resolver 1.1.1.1 1.0.0.1 valid=60s ipv6=off;
    location ~ ^/__cdnmnus_dynamic_vod/([A-Za-z0-9.-]+)(/.+)$ {
        internal;
        set $vod_dynamic_host $1;
        set $vod_dynamic_path $2;
        proxy_pass http://$vod_dynamic_host$vod_dynamic_path$is_args$args;
        proxy_set_header Host $vod_dynamic_host;
        proxy_cache cdnmnus_hls;
        proxy_cache_methods GET HEAD;
        proxy_cache_key "$host|dynamic-vod|$vod_dynamic_host|$vod_dynamic_path";
        proxy_cache_bypass $http_range;
        proxy_no_cache $http_range;
        proxy_cache_valid 200 30s;
        proxy_cache_lock on;
        proxy_cache_lock_timeout 2s;
        proxy_cache_lock_age 5s;
        proxy_buffering on;
        slice 1m;
        proxy_set_header Range $slice_range;
        proxy_read_timeout 300s;
        proxy_hide_header Location;
        proxy_hide_header Server;
    }
'''
    fallback_rules = ""
    lb_location = "" if not load_balancers else """
    location ^~ /__cdnmnus_lb__/ {
        internal;
        proxy_pass http://cdnmnus_load_balancers/;
        proxy_buffering on;
        proxy_cache cdnmnus_hls;
        proxy_cache_methods GET HEAD;
        proxy_cache_bypass $cdnmnus_skip_hls_cache;
        proxy_no_cache $cdnmnus_skip_hls_cache;
        proxy_cache_key "$scheme|$request_method|$host|$uri";
        proxy_cache_bypass $http_range;
        proxy_no_cache $http_range;
        proxy_cache_valid 200 302 30s;
        proxy_cache_lock on;
        proxy_cache_lock_timeout 15s;
        proxy_cache_lock_age 15s;
        proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
        proxy_intercept_errors off;
        proxy_read_timeout 300s;
        proxy_send_timeout 60s;
        add_header X-CDN-Route lb-hls always;
        add_header X-CDN-Cache $upstream_cache_status always;
    }
"""
    return f'''# Gerado pelo painel cdnmnus; não editar manualmente.
proxy_cache_path /var/cache/nginx/cdnmnus-hls levels=1:2 keys_zone=cdnmnus_hls:32m max_size=2g inactive=2m use_temp_path=off;

# Manifests podem conter credenciais no caminho. Eles nunca entram no cache
# em disco; apenas segmentos finitos (.ts/.m4s/.aac etc.) são compartilhados.
map $request_uri $cdnmnus_skip_hls_cache {{
    default 1;
    ~*\\.(?:ts|m4s|mp4|aac|mp3|vtt)(?:\\?|$) 0;
}}

map $request_uri $cdnmnus_refresh_action {{
    default refresh;
    ~^/(?:movie|series)/ refresh-vod;
}}

upstream {upstream_name} {{
    server {nginx_escape(server_host)}:80 max_fails=3 fail_timeout=10s;
    keepalive 32;
}}

upstream cdnmnus_token_broker {{
    server 127.0.0.1:9091;
    keepalive 16;
}}

upstream cdnmnus_resolved_origin {{
    server {nginx_escape(server_host)}:80 max_fails=3 fail_timeout=10s;
    keepalive 32;
}}

{resolved_upstreams}

{lb_upstream}{vod_upstreams}server {{
    listen 80;
    server_name {nginx_escape(config["public_host"])};
    location ^~ /.well-known/acme-challenge/ {{ root /var/www/html; }}
    location / {{ return 308 https://$host$request_uri; }}
}}

server {{
{tls_config}    server_name {nginx_escape(config["public_host"])};

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;

    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $server_addr;
    proxy_set_header X-Forwarded-For $server_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
    proxy_set_header Accept-Encoding "";

{chr(10).join(redirects)}
    proxy_hide_header Server;
    proxy_hide_header Via;
    proxy_hide_header X-Powered-By;
    proxy_redirect ~^https?://{nginx_escape(load_balancers[0] if load_balancers else "127.0.0.1")}(?::80)?/(.*)$ /__cdnmnus_lb__/$1;
{fallback_rules}

    sub_filter_once off;
    sub_filter_types application/vnd.apple.mpegurl application/x-mpegURL audio/mpegurl text/plain application/json application/octet-stream;
{chr(10).join(filters)}

    location = /nginx-health {{
        access_log off;
        default_type text/plain;
        return 200 "ok\\n";
    }}

    # Health composto para DNS/load balancer de múltiplas edges.
    location = /edge-health {{
        proxy_pass http://cdnmnus_token_broker/health;
        proxy_pass_request_body off;
        proxy_hide_header Server;
        proxy_hide_header Location;
        add_header Cache-Control no-store always;
    }}

    location = / {{
        root /var/www/mago-edge;
        try_files /index.html =404;
    }}

    location / {{
        proxy_pass http://{upstream_name};
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 60s;
    }}

    location ^~ /movie/ {{
        proxy_pass http://cdnmnus_token_broker;
        proxy_set_header X-Broker-Action resolve-vod;
        proxy_set_header X-Original-URI $request_uri;
        proxy_set_header Connection "";
        proxy_pass_request_body off;
        proxy_hide_header Server;
        proxy_hide_header Location;
        proxy_read_timeout 15s;
    }}

    location ^~ /series/ {{
        proxy_pass http://cdnmnus_token_broker;
        proxy_set_header X-Broker-Action resolve-vod;
        proxy_set_header X-Original-URI $request_uri;
        proxy_set_header Connection "";
        proxy_pass_request_body off;
        proxy_hide_header Server;
        proxy_hide_header Location;
        proxy_read_timeout 15s;
    }}

    # Segmentos HLS usam cache curto e cache lock para que espectadores do
    # mesmo canal nao multipliquem requisicoes identicas no XUI.
    location ^~ /hls/ {{
        proxy_pass http://cdnmnus_token_broker;
        proxy_set_header X-Broker-Action resolve;
        proxy_set_header X-Original-URI $request_uri;
        proxy_set_header Connection "";
        proxy_pass_request_body off;
        proxy_hide_header Server;
        proxy_hide_header Location;
        proxy_read_timeout 10s;
    }}

    location ^~ /live/ {{
        proxy_pass http://cdnmnus_token_broker;
        proxy_set_header X-Broker-Action resolve;
        proxy_set_header X-Original-URI $request_uri;
        proxy_set_header Connection "";
        proxy_pass_request_body off;
        proxy_hide_header Server;
        proxy_hide_header Location;
        proxy_read_timeout 10s;
    }}

    location ~ ^/[^/]+/[^/]+/[0-9]+\\.m3u8$ {{
        proxy_pass http://cdnmnus_token_broker;
        proxy_set_header X-Broker-Action resolve;
        proxy_set_header X-Original-URI $request_uri;
        proxy_set_header Connection "";
        proxy_pass_request_body off;
        proxy_hide_header Server;
        proxy_hide_header Location;
        proxy_read_timeout 10s;
    }}

    location ^~ /__cdnmnus_resolved_origin/ {{
        internal;
        proxy_pass http://cdnmnus_resolved_origin/;
        proxy_buffering on;
        proxy_cache cdnmnus_hls;
        proxy_cache_bypass $cdnmnus_skip_hls_cache;
        proxy_no_cache $cdnmnus_skip_hls_cache;
        proxy_cache_key "$scheme|$request_method|$host|$uri";
        proxy_cache_bypass $http_range;
        proxy_no_cache $http_range;
        proxy_cache_valid 200 6s;
        proxy_cache_lock on;
        proxy_cache_lock_timeout 2s;
        proxy_cache_lock_age 5s;
        proxy_read_timeout 20s;
        proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
        proxy_intercept_errors on;
        error_page 401 403 404 410 500 502 503 504 = @cdnmnus_refresh;
        proxy_hide_header Server;
        proxy_hide_header Location;
    }}

    location ^~ /__cdnmnus_retry_origin/ {{
        internal;
        proxy_pass http://cdnmnus_resolved_origin/;
        proxy_buffering on;
        proxy_cache cdnmnus_hls;
        proxy_cache_bypass $cdnmnus_skip_hls_cache;
        proxy_no_cache $cdnmnus_skip_hls_cache;
        proxy_cache_key "$scheme|$request_method|$host|$uri";
        proxy_cache_bypass $http_range;
        proxy_no_cache $http_range;
        proxy_cache_valid 200 6s;
        proxy_cache_lock on;
        proxy_cache_lock_timeout 2s;
        proxy_cache_lock_age 5s;
        proxy_read_timeout 20s;
        proxy_hide_header Server;
        proxy_hide_header Location;
    }}

    location @cdnmnus_refresh {{
        proxy_pass http://cdnmnus_token_broker;
        proxy_set_header X-Broker-Action $cdnmnus_refresh_action;
        proxy_set_header X-Original-URI $request_uri;
        proxy_set_header Connection "";
        proxy_pass_request_body off;
        proxy_hide_header Server;
        proxy_hide_header Location;
        proxy_read_timeout 10s;
    }}

{resolved_locations}

    # Relays fechados para sementes VOD explicitamente administradas.
    # Os prefixos sao internos e nao formam um proxy aberto.
{vod_locations}
{dynamic_vod_location}
{lb_location}}}
'''


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def apply_config(config: dict[str, Any]) -> None:
    old_config = config_from_db()
    old_include = NGINX_INCLUDE.read_text(encoding="utf-8") if NGINX_INCLUDE.exists() else None
    old_token_config = TOKEN_BROKER_CONFIG.read_text(encoding="utf-8") if TOKEN_BROKER_CONFIG.exists() else None
    replace_config(config)
    token_config = {
        "origin_host": config["upstream_host"],
        "public_host": config["public_host"],
        "load_balancers": config.get("load_balancers", []),
        "ttl_seconds": 15,
        "vod_hosts": configured_vod_hosts(),
    }
    atomic_write(TOKEN_BROKER_CONFIG, json.dumps(token_config, ensure_ascii=False) + "\n", 0o640)
    try:
        os.chown(TOKEN_BROKER_CONFIG, 0, grp.getgrnam("www-data").gr_gid)
    except (KeyError, PermissionError):
        pass
    atomic_write(NGINX_INCLUDE, render_include(config), 0o640)
    try:
        subprocess.run(["systemctl", "start", "cdnmnus-token-broker.service"], check=True, timeout=20, capture_output=True)
        result = subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            raise RuntimeError("nginx -t falhou")
        subprocess.run(["systemctl", "reload", "nginx"], check=True, timeout=20)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        if old_config:
            replace_config(old_config)
        else:
            replace_config({})
        if old_include is not None:
            atomic_write(NGINX_INCLUDE, old_include, 0o640)
        else:
            NGINX_INCLUDE.unlink(missing_ok=True)
        if old_token_config is not None:
            atomic_write(TOKEN_BROKER_CONFIG, old_token_config, 0o640)
            try:
                os.chown(TOKEN_BROKER_CONFIG, 0, grp.getgrnam("www-data").gr_gid)
            except (KeyError, PermissionError):
                pass
        else:
            TOKEN_BROKER_CONFIG.unlink(missing_ok=True)
        subprocess.run(["systemctl", "try-restart", "cdnmnus-token-broker.service"], check=False, timeout=20, capture_output=True)
        raise RuntimeError("aplicação falhou; configuração anterior restaurada")


def change_password(username: str, old_password: str, new_password: str) -> None:
    if len(new_password) < 12:
        raise ValueError("a nova senha deve possuir pelo menos 12 caracteres")
    user = get_user(username)
    if user is None or not verify_password(old_password, user["salt"], user["password_hash"]):
        raise ValueError("senha atual inválida")
    salt, digest = hash_password(new_password)
    with db_connect() as db:
        db.execute("UPDATE users SET salt=?,password_hash=?,must_change=0,updated_at=CURRENT_TIMESTAMP WHERE username=?", (salt, digest, username))


def basic_credentials(handler: BaseHTTPRequestHandler) -> tuple[str, str] | None:
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        return decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "cdnmnus-panel"

    def log_message(self, fmt: str, *args: object) -> None:
        # Nunca grava query strings, que podem conter credenciais de playlists.
        print(f"cdnmnus-panel: {self.command} {self.path.split('?', 1)[0]} - {fmt % args}")

    def require_auth(self) -> sqlite3.Row | None:
        user = authenticated_user(self)
        if user is not None:
            return user
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="cdnmnus-panel"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return None

    def do_GET(self) -> None:
        user = self.require_auth()
        if user is None:
            return
        route = self.path.split("?", 1)[0]
        if route in ("/api/xuiprofiles", "/api/profiles"):
            json_response(self, HTTPStatus.OK, api_profiles(config_from_db()))
            return
        if route == "/api/config":
            config = config_from_db()
            config["profiles"] = stored_profiles(config)
            config["must_change"] = bool(user["must_change"])
            json_response(self, HTTPStatus.OK, config)
            return
        if route != "/":
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "rota não encontrada"})
            return
        html_response(self, HTTPStatus.OK, """<!doctype html>
    <html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Mago CDN</title>
    <style>
    :root{color-scheme:dark;--bg:#0f172a;--panel:#111827;--panel-2:#172033;--line:#263247;--text:#f8fafc;--muted:#94a3b8;--blue:#2563eb;--orange:#f38020;--danger:#ef4444;--ok:#22c55e}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,textarea,select{font:inherit}button{border:0;border-radius:7px;padding:.7rem 1rem;background:var(--blue);color:white;font-weight:650;cursor:pointer}button:hover{filter:brightness(1.12)}button.ghost{background:transparent;border:1px solid var(--line);color:var(--muted)}button.danger{background:transparent;border:1px solid #7f1d1d;color:#fca5a5}.shell{display:flex;min-height:100vh}.sidebar{width:250px;border-right:1px solid var(--line);background:#0b1220;padding:25px 16px;flex:none}.brand{display:flex;align-items:center;gap:10px;font-size:20px;font-weight:800;margin:4px 10px 34px}.brand-mark{display:grid;place-items:center;width:32px;height:32px;border-radius:8px;background:var(--orange);color:#111827}.brand small{display:block;color:var(--muted);font-size:10px;font-weight:500;margin-top:2px}.nav{display:grid;gap:6px}.nav button{display:flex;align-items:center;gap:11px;width:100%;background:transparent;color:var(--muted);text-align:left;font-weight:600}.nav button.active{background:#17233a;color:white}.nav-icon{width:22px;text-align:center;color:var(--orange)}.content{width:min(1100px,100%);padding:34px clamp(18px,4vw,52px)}.topline{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:30px}.eyebrow{color:var(--orange);font-size:12px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.topline h1{font-size:30px;letter-spacing:0;margin:7px 0}.topline p{color:var(--muted);margin:0}.view{display:none;animation:rise .24s ease both}.view.active{display:block}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:22px;box-shadow:0 12px 28px #02061733}.card h2,.card h3{margin:0 0 7px}.card h2{font-size:18px}.card h3{font-size:14px}.card p,.hint{color:var(--muted);line-height:1.5}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:20px 0}.stat{background:var(--panel-2);border:1px solid var(--line);padding:16px;border-radius:7px}.stat strong{display:block;font-size:24px}.stat span{color:var(--muted);font-size:12px}.profile-row{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-top:18px;padding:15px;background:var(--panel-2);border:1px solid var(--line);border-radius:7px}.profile-row strong{display:block}.profile-row span{display:block;color:var(--muted);font-size:13px;margin-top:4px}.status{display:inline-flex;align-items:center;gap:6px;color:#86efac;font-size:12px;font-weight:700}.status:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px #22c55e22}.field{margin:17px 0}.field label{display:block;font-size:13px;color:#cbd5e1;font-weight:650;margin-bottom:7px}.field input,.field textarea,.field select,.select{width:100%;background:#0b1220;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:.78rem .85rem;outline:0}.field input:focus,.field textarea:focus,.field select:focus{border-color:#3b82f6;box-shadow:0 0 0 3px #2563eb26}.field textarea{min-height:125px;resize:vertical}.actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:22px}.tabs-title{margin-bottom:18px}.toast{position:fixed;right:22px;bottom:22px;max-width:min(420px,calc(100vw - 44px));padding:14px 17px;border-radius:7px;background:#172033;border:1px solid var(--line);box-shadow:0 16px 34px #02061788;transform:translateY(20px);opacity:0;pointer-events:none;transition:.2s}.toast.show{transform:none;opacity:1}.toast.error{border-color:#7f1d1d;color:#fecaca}.toast.ok{border-color:#166534;color:#bbf7d0}.bar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}.bar h2{margin:0;font-size:18px}.skeleton{height:76px;margin-top:18px;border-radius:7px;background:linear-gradient(90deg,#172033,#263247,#172033);background-size:200% 100%;animation:shine 1.3s infinite;color:transparent}@keyframes shine{to{background-position:-200% 0}}@keyframes rise{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}@media(max-width:760px){.shell{display:block}.sidebar{width:100%;padding:15px;border-right:0;border-bottom:1px solid var(--line)}.brand{margin:2px 4px 15px}.nav{display:flex;overflow:auto}.nav button{white-space:nowrap;width:auto;padding:.6rem .75rem}.content{padding:25px 16px}.topline{display:block}.topline h1{font-size:25px}.grid{grid-template-columns:1fr}.stats{grid-template-columns:repeat(3,1fr)}.profile-row{align-items:flex-start;flex-direction:column}}
    </style></head><body><div class="shell"><aside class="sidebar"><div class="brand"><span class="brand-mark">M</span><span>Mago CDN<small>ADMIN CONSOLE</small></span></div><nav class="nav"><button class="active" data-tab="overview"><span class="nav-icon">&#9632;</span>XUIs cadastrados</button><button data-tab="config"><span class="nav-icon">&#9881;</span>Novo cadastro</button><button data-tab="security"><span class="nav-icon">&#9679;</span>Minha conta</button></nav></aside><main class="content"><header class="topline"><div><div class="eyebrow">Mago CDN / Console</div><h1 id="page-title">XUIs cadastrados</h1><p>Controle os perfis autorizados e seus caminhos de distribuição.</p></div><span class="status">Serviços protegidos</span></header>
    <section class="view active" id="overview"><div class="bar"><h2>Visão geral</h2><button id="quick-new">+ Novo perfil</button></div><div class="stats"><div class="stat"><strong id="profile-count">0</strong><span>Perfis XUI</span></div><div class="stat"><strong id="lb-count">0</strong><span>Load balancers</span></div><div class="stat"><strong id="port-count">-</strong><span>Porta do upstream</span></div></div><div class="card"><h2>Perfis de distribuição</h2><p>Um perfil ativo atende o domínio público. Edite os dados ou remova perfis que não são mais utilizados.</p><div id="profile-list"><div class="skeleton">Carregando perfis</div></div></div></section>
    <section class="view" id="config"><div class="tabs-title"><div class="eyebrow">Configuração</div><h2 id="form-title">Novo perfil XUI</h2></div><form class="card" id="f"><div class="field"><label>Nome do perfil</label><input name="name" required placeholder="XUI principal"></div><div class="grid"><div class="field"><label>IP ou DNS do XUI</label><input name="upstream_host" required placeholder="xui.exemplo.com ou 203.0.113.10"></div><div class="field"><label>Porta HTTP</label><input name="upstream_port" type="number" value="80" min="1" max="65535" required></div></div><div class="field"><label>Domínio público da VPS</label><input name="public_host" required placeholder="cdn.exemplo.com"></div><div class="field"><label>Load balancers HTTP</label><textarea name="load_balancers" placeholder="Um IP ou DNS por linha"></textarea><span class="hint">Usados quando o main redirecionar ou falhar. O painel valida cada entrada antes de aplicar.</span></div><div class="actions"><button type="submit">Salvar e aplicar</button><button type="button" class="ghost" id="cancel-edit">Cancelar</button></div></form></section>
    <section class="view" id="security"><div class="tabs-title"><div class="eyebrow">Segurança</div><h2>Minha conta</h2></div><div class="card"><h2>Credenciais do administrador</h2><p>O usuário de acesso é gerenciado pelo serviço. A senha é armazenada somente como hash protegido.</p><div class="field"><label>Usuário atual</label><input value="Administrador do painel" readonly></div><form id="p"><div class="field"><label>Senha atual</label><input name="current_password" type="password" required autocomplete="current-password"></div><div class="field"><label>Nova senha</label><input id="new-password" name="new_password" type="password" minlength="12" required autocomplete="new-password"><span class="hint" id="strength">Mínimo de 12 caracteres.</span></div><div class="actions"><button type="submit">Atualizar senha</button></div></form></div></section>
    <div class="toast" id="toast"></div></main></div><script>
    const $=s=>document.querySelector(s), tabs=[...document.querySelectorAll('[data-tab]')];let current={},items=[],editing='',refreshController;
    const toast=(message,ok=true)=>{const el=$('#toast');el.textContent=message;el.className='toast show '+(ok?'ok':'error');clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>el.className='toast',4200)};
    const showTab=name=>{tabs.forEach(x=>x.classList.toggle('active',x.dataset.tab===name));document.querySelectorAll('.view').forEach(x=>x.classList.toggle('active',x.id===name));const titles={overview:'XUIs cadastrados',config:'Novo cadastro',security:'Minha conta'};$('#page-title').textContent=titles[name]};tabs.forEach(x=>x.onclick=()=>showTab(x.dataset.tab));
    const fill=x=>{const form=$('#f');form.name.value=x.name||'';form.upstream_host.value=x.upstream_host||'';form.public_host.value=x.public_host||current.public_host||'';form.upstream_port.value=x.upstream_port||80;form.load_balancers.value=(x.load_balancers||[]).join('\n');editing=x.profile_id||'';$('#form-title').textContent=editing?'Editar perfil XUI':'Novo perfil XUI'};
    const refresh=async()=>{const r=await fetch('./api/config');if(!r.ok){toast('Não foi possível carregar a configuração.',false);return}current=await r.json();items=current.profiles||[];$('#profile-count').textContent=items.length;$('#lb-count').textContent=items.reduce((n,x)=>n+(x.load_balancers||[]).length,0);const list=$('#profile-list');list.replaceChildren(...items.map(x=>{const row=document.createElement('div');row.className='profile-row';const info=document.createElement('div');const title=document.createElement('strong');title.textContent=x.name||'Perfil sem nome';const meta=document.createElement('span');meta.textContent=(x.load_balancers||[]).length+' load balancer(s) configurado(s)';info.append(title,meta);const actions=document.createElement('div');const edit=document.createElement('button');edit.className='ghost';edit.textContent='Editar';edit.onclick=()=>{fill(x);showTab('config')};const status=document.createElement('span');status.className='status';status.textContent=x.profile_id===current.active_profile_id?'Ativo':'Disponível';actions.append(edit,status);row.append(info,actions);return row}));if(current.must_change)toast('Troque a senha inicial antes de alterar a configuração.',false)};
    $('#quick-new').onclick=()=>{fill({});showTab('config')};$('#cancel-edit').onclick=()=>{fill(items.find(x=>x.profile_id===current.active_profile_id)||{});showTab('overview')};$('#f').onsubmit=async e=>{e.preventDefault();const x=Object.fromEntries(new FormData(e.target));x.profile_id=editing;x.upstream_port=Number(x.upstream_port);const r=await fetch('./api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)});const data=await r.json();if(!r.ok){toast(data.error||'Não foi possível salvar.',false);return}toast('Perfil salvo e aplicado.');await refresh();showTab('overview')};$('#p').onsubmit=async e=>{e.preventDefault();const r=await fetch('./api/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});const data=await r.json();if(!r.ok){toast(data.error||'Não foi possível atualizar a senha.',false);return}toast('Senha atualizada. Autentique novamente.');e.target.reset()};$('#new-password').oninput=e=>$('#strength').textContent=e.target.value.length>=12?'Força mínima atendida.':'Mínimo de 12 caracteres.';fill({});refresh();
    const liveRefresh=async()=>{const list=$('#profile-list');if(!items.length)list.innerHTML='<div class="skeleton">Carregando perfis</div>';if(refreshController)refreshController.abort();refreshController=new AbortController();const timeout=setTimeout(()=>refreshController.abort(),8000);try{const response=await fetch('/api/xuiprofiles',{cache:'no-store',signal:refreshController.signal});if(!response.ok)throw new Error(`API de perfis retornou HTTP ${response.status}`);const data=await response.json();if(!data||!Array.isArray(data.profiles))throw new Error('Resposta da API sem o array profiles');items=data.profiles;current.active_profile_id=data.active_profile_id||'';$('#profile-count').textContent=items.length;$('#lb-count').textContent=items.reduce((sum,item)=>sum+(Number(item.load_balancer_count)|| (Array.isArray(item.load_balancers)?item.load_balancers.length:0)),0);const active=items.find(item=>item.profile_id===current.active_profile_id)||items[0];$('#port-count').textContent=active?.upstream_port||'-';if(!items.length){list.innerHTML='<div class="hint">Nenhum perfil cadastrado.</div>';return}list.replaceChildren(...items.map(item=>{const row=document.createElement('div');row.className='profile-row';const info=document.createElement('div');const name=document.createElement('strong');name.textContent=item.name||'Perfil sem nome';const details=document.createElement('span');details.textContent=`${item.public_host||'domínio não informado'} | ${item.upstream_host||'upstream não informado'}:${item.upstream_port||'-'} | ${item.load_balancer_count||0} LB(s)`;info.append(name,details);const actions=document.createElement('div');const edit=document.createElement('button');edit.className='ghost';edit.textContent='Editar';edit.onclick=()=>{fill(item);showTab('config')};const reload=document.createElement('button');reload.className='ghost';reload.textContent='Recarregar proxy';reload.onclick=async()=>{try{const result=await fetch('/api/reload',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const payload=await result.json();toast(payload.message||payload.error,!payload.error)}catch(error){toast('Falha ao recarregar o proxy.',false)}};const remove=document.createElement('button');remove.className='danger';remove.textContent='Excluir';remove.onclick=async()=>{if(!confirm('Excluir este perfil e seus load balancers?'))return;const result=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'delete',profile_id:item.profile_id})});const payload=await result.json();if(!result.ok){toast(payload.error||'Não foi possível excluir.',false);return}toast('Perfil excluído e proxy atualizado.');await liveRefresh()};const status=document.createElement('span');status.className='status';status.textContent=item.status||'Inativo';actions.append(edit,reload,remove,status);row.append(info,actions);return row}))}catch(error){console.error('Falha ao carregar perfis:',error);list.innerHTML=`<div class="hint">Não foi possível carregar os perfis agora. ${error.name==='AbortError'?'A API demorou mais de 8 segundos.':'Verifique a autenticação e o console.'}</div>`;toast('Falha ao carregar perfis.',false)}finally{clearTimeout(timeout)}};liveRefresh();setInterval(liveRefresh,30000);
    </script></body></html>""")

    def do_POST(self) -> None:
        user = self.require_auth()
        if user is None:
            return
        route = self.path.split("?", 1)[0]
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Content-Type deve ser application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("corpo inválido")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            credentials = basic_credentials(self)
            if credentials is None:
                raise ValueError("autenticação inválida")
            if route == "/api/password":
                change_password(user["username"], credentials[1], str(payload.get("new_password", "")))
                json_response(self, HTTPStatus.OK, {"ok": True, "message": "senha alterada; autentique novamente"})
                return
            if route == "/api/reload":
                result = subprocess.run(["nginx", "-t"], capture_output=True, timeout=20)
                if result.returncode != 0:
                    raise RuntimeError("nginx -t falhou; proxy não foi recarregado")
                subprocess.run(["systemctl", "reload", "nginx"], check=True, timeout=20)
                json_response(self, HTTPStatus.OK, {"ok": True, "message": "proxy recarregado"})
                return
            if route != "/api/config":
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "rota não encontrada"})
                return
            if user["must_change"]:
                json_response(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": "troque a senha inicial antes de alterar o upstream"})
                return
            if payload.get("action") == "delete":
                config = delete_profile(str(payload.get("profile_id", "")))
                message = "perfil excluído e próximo perfil ativado"
            else:
                config = save_profile(payload)
                message = "perfil validado, salvo e aplicado"
            json_response(self, HTTPStatus.OK, {"ok": True, "message": message, "profile_id": config["profile_id"], "profile_count": len(config["profiles"])})
        except Exception as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})


def main() -> None:
    initialize_db()
    print(f"cdnmnus-panel ouvindo em {BIND}:{PORT}")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
