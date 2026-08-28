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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BIND = os.environ.get("CDNMNUS_PANEL_BIND", "127.0.0.1")
PORT = int(os.environ.get("CDNMNUS_PANEL_PORT", "9090"))
DB_PATH = Path(os.environ.get("CDNMNUS_PANEL_DB", "/etc/cdnmnus/panel.db"))
NGINX_INCLUDE = Path(os.environ.get("CDNMNUS_NGINX_INCLUDE", "/etc/nginx/conf.d/99-cdnmnus-upstream.conf"))
PUBLIC_HOST = os.environ.get("CDNMNUS_PUBLIC_HOST", "")
PANEL_USER = os.environ.get("CDNMNUS_PANEL_USER", "admin")
PANEL_PASSWORD = os.environ.get("CDNMNUS_PANEL_PASSWORD", "")
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
    handler.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
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


def normalize_config(payload: dict[str, Any]) -> dict[str, Any]:
    host = str(payload.get("upstream_host", "")).strip()
    port = payload.get("upstream_port", 80)
    public_host = str(payload.get("public_host", PUBLIC_HOST)).strip()
    if not valid_host(host):
        raise ValueError("upstream_host deve ser um IP ou DNS válido")
    if not isinstance(port, int) or port != 80:
        raise ValueError("o upstream deve usar HTTP na porta 80 neste perfil")
    if public_host and not DOMAIN_RE.fullmatch(public_host):
        raise ValueError("public_host contém caracteres inválidos")
    addresses = resolve_host(host)
    return {
        "scheme": "http",
        "upstream_host": host,
        "upstream_port": port,
        "resolved_addresses": addresses,
        "public_host": public_host or "_",
    }


def nginx_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


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


def render_include(config: dict[str, Any]) -> str:
    host = str(config["upstream_host"])
    server_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    upstream_name = "cdnmnus_dynamic_backend"
    candidates = [host, *config.get("resolved_addresses", [])]
    filters: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not valid_host(candidate):
            continue
        url_host = f"[{candidate}]" if ":" in candidate and not candidate.startswith("[") else candidate
        for prefix, replacement_prefix in (("http://", "http://"), ("https://", "http://"), ("//", "//")):
            for suffix in (":80", ""):
                source = f"{prefix}{url_host}{suffix}"
                replacement = f"{replacement_prefix}$host"
                line = f'    sub_filter "{nginx_escape(source)}" "{nginx_escape(replacement)}";'
                if line not in seen:
                    seen.add(line)
                    filters.append(line)
    cert_name = str(config.get("public_host", ""))
    cert_dir = Path("/etc/letsencrypt/live") / cert_name if cert_name and cert_name != "_" else Path("/nonexistent")
    tls = (cert_dir / "fullchain.pem").exists() and (cert_dir / "privkey.pem").exists()
    listen_tls = "    listen 443 ssl;\n    ssl_certificate /etc/letsencrypt/live/%s/fullchain.pem;\n    ssl_certificate_key /etc/letsencrypt/live/%s/privkey.pem;\n    ssl_protocols TLSv1.2 TLSv1.3;\n" % (nginx_escape(cert_name), nginx_escape(cert_name)) if tls else ""
    return f'''# Gerado pelo painel cdnmnus; não editar manualmente.
upstream {upstream_name} {{
    server {nginx_escape(server_host)}:80 max_fails=3 fail_timeout=10s;
    keepalive 32;
}}

server {{
    listen 80;
{listen_tls}    server_name {nginx_escape(config["public_host"])};

    proxy_http_version 1.1;
    proxy_set_header Host {nginx_escape(host)};
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
    proxy_set_header Accept-Encoding "";

    proxy_hide_header Location;
    proxy_hide_header Server;
    proxy_hide_header Via;
    proxy_hide_header X-Powered-By;
    proxy_redirect off;

    sub_filter_once off;
    sub_filter_types application/vnd.apple.mpegurl application/x-mpegURL audio/mpegurl text/plain application/json application/octet-stream;
{chr(10).join(filters)}

    location = /admin {{
        return 301 /admin/;
    }}

    location ^~ /admin/ {{
        proxy_pass http://127.0.0.1:9090/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_hide_header Server;
        proxy_read_timeout 30s;
    }}

    location = /nginx-health {{
        access_log off;
        default_type text/plain;
        return 200 "ok\\n";
    }}

    location / {{
        proxy_pass http://{upstream_name};
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 60s;
    }}
}}
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
    save_config(config)
    atomic_write(NGINX_INCLUDE, render_include(config), 0o640)
    result = subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        if old_config:
            save_config(old_config)
        if old_include is not None:
            atomic_write(NGINX_INCLUDE, old_include, 0o640)
        else:
            NGINX_INCLUDE.unlink(missing_ok=True)
        raise RuntimeError("nginx -t falhou; configuração anterior restaurada")
    subprocess.run(["systemctl", "reload", "nginx"], check=True, timeout=20)


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
        if route == "/api/config":
            config = config_from_db()
            config["must_change"] = bool(user["must_change"])
            json_response(self, HTTPStatus.OK, config)
            return
        html_response(self, HTTPStatus.OK, """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>cdnmnus admin</title>
<style>body{font:16px system-ui;max-width:760px;margin:3rem auto;padding:0 1rem}section{border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}input{width:100%;box-sizing:border-box;padding:.7rem;margin:.3rem 0 1rem}button{padding:.7rem 1rem;cursor:pointer}pre{white-space:pre-wrap;background:#f6f6f6;padding:1rem;border-radius:8px}</style>
<h1>cdnmnus admin</h1><p>Configure somente um XUI autorizado. O proxy público não encaminha o painel para o upstream.</p>
<section><h2>Upstream HTTP</h2><form id=f><label>IP ou DNS do XUI<br><input name=upstream_host required placeholder="xui.exemplo.com ou 203.0.113.10"></label><label>Porta<br><input name=upstream_port type=number value=80 min=1 max=65535></label><label>Domínio/IP público da VPS<br><input name=public_host required placeholder="cdn.exemplo.com"></label><button>Validar e aplicar</button></form></section>
<section><h2>Trocar senha</h2><form id=p><label>Senha atual<br><input name=current_password type=password required></label><label>Nova senha (mínimo 12 caracteres)<br><input name=new_password type=password minlength=12 required></label><button>Trocar senha</button></form></section><pre id=o></pre>
<script>const o=document.querySelector('#o');const cfg=async()=>{let r=await fetch('./api/config');if(r.ok){let x=await r.json();f.upstream_host.value=x.upstream_host||'';f.public_host.value=x.public_host||'';f.upstream_port.value=x.upstream_port||80;if(x.must_change)o.textContent='Troque a senha inicial antes de alterar o upstream.'}};cfg();f.onsubmit=async e=>{e.preventDefault();let x=Object.fromEntries(new FormData(f));x.upstream_port=Number(x.upstream_port);let r=await fetch('./api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)});o.textContent=JSON.stringify(await r.json(),null,2)};p.onsubmit=async e=>{e.preventDefault();let r=await fetch('./api/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(p)))});o.textContent=JSON.stringify(await r.json(),null,2);if(r.ok)p.reset()};</script>""")

    def do_POST(self) -> None:
        user = self.require_auth()
        if user is None:
            return
        route = self.path.split("?", 1)[0]
        try:
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
            if route != "/api/config":
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "rota não encontrada"})
                return
            if user["must_change"]:
                json_response(self, HTTPStatus.FORBIDDEN, {"ok": False, "error": "troque a senha inicial antes de alterar o upstream"})
                return
            config = normalize_config(payload)
            apply_config(config)
            json_response(self, HTTPStatus.OK, {"ok": True, "message": "upstream validado e aplicado", "resolved_count": len(config["resolved_addresses"])})
        except Exception as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})


def main() -> None:
    initialize_db()
    print(f"cdnmnus-panel ouvindo em {BIND}:{PORT}")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
