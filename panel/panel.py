#!/usr/bin/env python3
"""cdnmnus: painel mínimo e autenticado para um upstream HTTP autorizado.

Este serviço não busca nem armazena credenciais de playlists. Ele apenas configura
host/porta de um upstream e gera um include do Nginx para o administrador local.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BIND = os.environ.get("CDNMNUS_PANEL_BIND", "127.0.0.1")
PORT = int(os.environ.get("CDNMNUS_PANEL_PORT", "9090"))
CONFIG_PATH = Path(os.environ.get("CDNMNUS_PANEL_CONFIG", "/etc/cdnmnus/upstream.json"))
NGINX_INCLUDE = Path(os.environ.get("CDNMNUS_NGINX_INCLUDE", "/etc/nginx/conf.d/99-cdnmnus-upstream.conf"))
PUBLIC_HOST = os.environ.get("CDNMNUS_PUBLIC_HOST", "")
PANEL_USER = os.environ.get("CDNMNUS_PANEL_USER", "admin")
PANEL_PASSWORD = os.environ.get("CDNMNUS_PANEL_PASSWORD", "")
MAX_BODY = 16 * 1024

HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.*_-]+$")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
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
    handler.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
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


def normalize_config(payload: dict) -> dict:
    host = str(payload.get("upstream_host", "")).strip()
    port = payload.get("upstream_port", 80)
    public_host = str(payload.get("public_host", PUBLIC_HOST)).strip()
    if not valid_host(host):
        raise ValueError("upstream_host deve ser um IP ou DNS válido")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("upstream_port deve estar entre 1 e 65535")
    if port != 80:
        raise ValueError("o XUI deve usar HTTP na porta 80 neste perfil")
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
    return value.replace("\\", "\\\\").replace("\"", '\\"')


def render_include(config: dict) -> str:
    host = config["upstream_host"]
    server_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    upstream_name = "cdnmnus_dynamic_backend"
    # O XUI pode devolver o DNS configurado ou o IP resolvido em playlists.
    # Gera filtros para ambos, sem registrar tokens ou query strings.
    filter_lines: list[str] = []
    seen_filters: set[str] = set()
    for candidate in [host, *config.get("resolved_addresses", [])]:
        if not isinstance(candidate, str) or not valid_host(candidate):
            continue
        url_host = f"[{candidate}]" if ":" in candidate and not candidate.startswith("[") else candidate
        for prefix, replacement_prefix in (("http://", "http://"), ("https://", "http://"), ("//", "//")):
            for suffix in (":80", ""):
                source = f"{prefix}{url_host}{suffix}"
                replacement = f"{replacement_prefix}$host"
                line = f'    sub_filter "{nginx_escape(source)}" "{nginx_escape(replacement)}";'
                if line not in seen_filters:
                    seen_filters.add(line)
                    filter_lines.append(line)
    filters = "\n".join(filter_lines)
    # Host encaminhado ao upstream é usado somente na conexão interna.
    return f'''# Gerado pelo painel cdnmnus; não editar manualmente.
# Upstream lógico configurado: {nginx_escape(host)} (não é publicado ao cliente)
upstream {upstream_name} {{
    server {nginx_escape(server_host)}:80 max_fails=3 fail_timeout=10s;
    keepalive 32;
}}

server {{
    listen 80;
    server_name {nginx_escape(config["public_host"])};

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

    # Reescreve somente URLs textuais que contenham o host configurado.
    # Conteúdos opacos, URLs assinadas ou domínios alternativos exigem revisão.
    sub_filter_once off;
    sub_filter_types application/vnd.apple.mpegurl application/x-mpegURL audio/mpegurl text/plain application/json application/octet-stream;
{filters}

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


def apply_config(config: dict) -> None:
    atomic_write(CONFIG_PATH, json.dumps(config, indent=2, ensure_ascii=False) + "\n", 0o600)
    atomic_write(NGINX_INCLUDE, render_include(config), 0o640)
    result = subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError("nginx -t falhou; configuração não foi ativada")
    subprocess.run(["systemctl", "reload", "nginx"], check=True, timeout=20)


def current_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class Handler(BaseHTTPRequestHandler):
    server_version = "cdnmnus-panel"

    def log_message(self, fmt: str, *args: object) -> None:
        # Não grava query strings, que podem conter credenciais de playlists.
        print(f"cdnmnus-panel: {self.command} {self.path.split('?', 1)[0]} - {fmt % args}")

    def authorized(self) -> bool:
        if not PANEL_PASSWORD:
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            user, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(user, PANEL_USER) and hmac.compare_digest(password, PANEL_PASSWORD)

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="cdnmnus-panel"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def do_GET(self) -> None:
        if not self.require_auth():
            return
        if self.path.split("?", 1)[0] == "/api/config":
            json_response(self, HTTPStatus.OK, current_config())
            return
        html_response(self, HTTPStatus.OK, """<!doctype html><meta charset=utf-8><title>cdnmnus upstream</title>
<style>body{font:16px system-ui;max-width:680px;margin:3rem auto;padding:0 1rem}input{width:100%;padding:.6rem;margin:.25rem 0 1rem}button{padding:.7rem 1rem}pre{white-space:pre-wrap}</style>
<h1>Configurar upstream HTTP</h1><p>Use somente XUI autorizado. O destino fica no servidor e não aparece no formulário público.</p>
<form id=f><label>IP ou DNS do upstream<br><input name=upstream_host required placeholder="xui.exemplo.com ou 203.0.113.10"></label>
<label>Porta<br><input name=upstream_port type=number value=80 min=1 max=65535></label>
<label>Host público da VPS<br><input name=public_host placeholder="vps.exemplo.com"></label><button>Validar e aplicar</button></form><pre id=o></pre>
<script>f.onsubmit=async e=>{e.preventDefault();let x=Object.fromEntries(new FormData(f));x.upstream_port=Number(x.upstream_port);o.textContent=JSON.stringify(await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)}).then(r=>r.json()),null,2)}</script>""")

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        if self.path.split("?", 1)[0] != "/api/config":
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "rota não encontrada"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("corpo inválido")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            config = normalize_config(payload)
            apply_config(config)
            json_response(self, HTTPStatus.OK, {"ok": True, "message": "upstream validado e aplicado", "resolved_addresses": config["resolved_addresses"]})
        except Exception as exc:  # resposta administrativa sem stack trace
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})


def main() -> None:
    if not PANEL_PASSWORD:
        raise SystemExit("CDNMNUS_PANEL_PASSWORD não configurada; painel não será iniciado")
    print(f"cdnmnus-panel ouvindo em {BIND}:{PORT}")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
