"""Renderização pura e determinística dos vhosts Nginx por tenant."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from core.db import normalize_hostname, normalize_id, normalize_port


@dataclass(frozen=True)
class RenderedTenant:
    tenant_id: str
    relative_path: str
    content: str
    sha256: str


def _nginx(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _normalized(tenant: dict[str, Any]) -> dict[str, Any]:
    tenant_id = normalize_id(str(tenant["id"]), "tenant_id")
    hosts = tenant.get("hosts", [])
    aliases = sorted({normalize_hostname(str(item["hostname"] if isinstance(item, dict) else item))
                      for item in hosts})
    canonical = normalize_hostname(str(tenant["canonical_host"]))
    aliases = [canonical, *[host for host in aliases if host != canonical]]
    upstreams = tenant.get("upstreams", [])
    origin = [item for item in upstreams if item["kind"] == "origin"]
    if len(origin) != 1:
        raise ValueError(f"tenant {tenant_id} deve ter exatamente uma origem")
    grouped = {kind: [] for kind in ("lb", "vod")}
    for item in upstreams:
        if item["kind"] in grouped:
            grouped[item["kind"]].append({"host": normalize_hostname(str(item["host"])), "port": normalize_port(item["port"])})
    return {"id": tenant_id, "hosts": aliases,
            "origin": {"host": normalize_hostname(str(origin[0]["host"])), "port": normalize_port(origin[0]["port"])},
            "lb": sorted(grouped["lb"], key=lambda x: (x["host"], x["port"])),
            "vod": sorted(grouped["vod"], key=lambda x: (x["host"], x["port"]))}


def render_tenant(tenant: dict[str, Any]) -> RenderedTenant:
    cfg = _normalized(tenant)
    tid = cfg["id"]
    origin = cfg["origin"]
    server_names = " ".join(_nginx(host) for host in cfg["hosts"])
    lb_upstreams = "\n\n".join(
        f"upstream lb_{tid}_{index} {{\n    server {_nginx(item['host'])}:{item['port']};\n    keepalive 32;\n}}"
        for index, item in enumerate(cfg["lb"])
    )
    lb_locations = "\n\n".join(
        f'''    location ^~ /__cdnmnus_{tid}_lb_{index}/ {{
        internal;
        proxy_pass http://lb_{tid}_{index}/;
        proxy_cache cache_{tid};
        proxy_cache_key "{tid}|lb{index}|$request_method|$host|$request_uri";
        proxy_cache_lock on;
        proxy_hide_header Location;
        proxy_hide_header Server;
    }}'''
        for index, _ in enumerate(cfg["lb"])
    )
    vod_relay_upstream = f'''upstream vod_relay_{tid} {{
    server unix:/run/cdnmnus/vod-relay-{tid}.sock;
    keepalive 32;
}}
''' if cfg["vod"] else ""
    vod_public_location = f'''    location ~ ^/(?:movie|series)/ {{
        limit_except GET HEAD {{ deny all; }}
        proxy_pass http://vod_relay_{tid};
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header X-CDN-Public-Host $host;
        proxy_set_header Range $http_range;
        proxy_set_header If-Range $http_if_range;
        proxy_set_header X-Forwarded-For "";
        proxy_set_header X-Real-IP "";
        proxy_hide_header Location;
        proxy_hide_header Server;
        proxy_hide_header Via;
        proxy_hide_header X-Powered-By;
        proxy_hide_header X-Accel-Redirect;
        proxy_hide_header Set-Cookie;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 30s;
        access_log off;
    }}''' if cfg["vod"] else '''    location ~ ^/(?:movie|series)/ {
        access_log off;
        return 503;
    }'''
    content = f'''# Gerado pelo cdnmnus; não editar manualmente.
proxy_cache_path /var/cache/nginx/cdnmnus/{tid} levels=1:2
    keys_zone=cache_{tid}:32m max_size=2g inactive=2m use_temp_path=off;

upstream origin_{tid} {{
    server {_nginx(origin['host'])}:{origin['port']};
    keepalive 32;
}}

upstream broker_{tid} {{
    server unix:/run/cdnmnus/broker-{tid}.sock;
    keepalive 16;
}}

{vod_relay_upstream}

{lb_upstreams}

server {{
    listen 80;
    server_name {server_names};
    location ^~ /.well-known/acme-challenge/ {{ root /var/www/html; }}
    location / {{ return 308 https://$host$request_uri; }}
}}

server {{
    listen 443 ssl;
    server_name {server_names};
    ssl_certificate /etc/letsencrypt/live/{_nginx(cfg['hosts'][0])}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{_nginx(cfg['hosts'][0])}/privkey.pem;

    location = /edge-health {{
        proxy_pass http://broker_{tid}/health;
        proxy_pass_request_body off;
        access_log off;
    }}

    location ~ ^/(?:hls|live)/ {{
        proxy_pass http://broker_{tid};
        proxy_set_header X-CDN-Tenant {tid};
        proxy_set_header X-CDN-Public-Host $host;
        proxy_set_header X-Broker-Action resolve;
        proxy_set_header X-Original-URI $request_uri;
        proxy_pass_request_body off;
    }}

    # Manifestos emitidos pelo XUI usam /usuario/senha/id.m3u8. Eles também
    # precisam passar pelo broker para refresh e fail-closed.
    location ~ ^/[^/]+/[^/]+/[0-9]+\.m3u8$ {{
        proxy_pass http://broker_{tid};
        proxy_set_header X-CDN-Tenant {tid};
        proxy_set_header X-CDN-Public-Host $host;
        proxy_set_header X-Broker-Action resolve;
        proxy_set_header X-Original-URI $request_uri;
        proxy_pass_request_body off;
    }}

{vod_public_location}

    location ^~ /__cdnmnus_{tid}_origin/ {{
        internal;
        proxy_pass http://origin_{tid}/;
        proxy_cache cache_{tid};
        # Tokens variam por segmento; não podem fragmentar o cache nem virar
        # parte do identificador persistido. Range (VOD) nunca é cacheado.
        proxy_cache_key "{tid}|$request_method|$host|$uri";
        proxy_cache_bypass $http_range;
        proxy_no_cache $http_range;
        proxy_cache_lock on;
        proxy_cache_lock_timeout 2s;
        proxy_cache_lock_age 5s;
        proxy_read_timeout 20s;
        proxy_hide_header Location;
        proxy_hide_header Server;
    }}

{lb_locations}

    location = / {{
        default_type text/html;
        add_header Cache-Control "no-store" always;
        return 200 "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='robots' content='noindex'><meta name='color-scheme' content='dark'><title>Mago Edge Infrastructure</title><style>body{{margin:0;background:#080d18;color:#eef4ff;font:16px system-ui,sans-serif;display:grid;place-items:center;min-height:100vh}}main{{max-width:760px;width:calc(100% - 48px);padding:48px;min-height:55vh;display:flex;flex-direction:column;justify-content:center}}h1{{font-size:clamp(36px,6vw,64px);line-height:1.05;margin:0 0 18px;letter-spacing:-.03em}}p{{color:#94a3b8;line-height:1.6}}.ok{{color:#22c55e;font-weight:600;letter-spacing:.02em}}footer{{margin-top:56px;padding-top:18px;border-top:1px solid #223047;color:#64748b;font-size:13px;letter-spacing:.02em}}</style></head><body><main><p class='ok'>● Edge node active</p><h1>Content delivery at the edge.</h1><p>Protected edge network. Direct access is restricted by edge security policies.</p><footer>2026 @MagoPD todos os direitos reservados.</footer></main></body></html>";
    }}

    location / {{
        proxy_pass http://origin_{tid};
        proxy_set_header Host $host;
        proxy_hide_header Location;
        proxy_hide_header Server;
    }}
}}
'''
    digest = hashlib.sha256(content.encode()).hexdigest()
    return RenderedTenant(tid, f"tenants/{tid}.conf", content, digest)


def render_all(tenants: Iterable[dict[str, Any]]) -> dict[str, RenderedTenant]:
    rendered: dict[str, RenderedTenant] = {}
    all_hosts: set[str] = set()
    for tenant in sorted(tenants, key=lambda item: str(item["id"])):
        normalized = _normalized(tenant)
        overlap = all_hosts.intersection(normalized["hosts"])
        if overlap:
            raise ValueError(f"hostname duplicado entre tenants: {sorted(overlap)[0]}")
        all_hosts.update(normalized["hosts"])
        item = render_tenant(tenant)
        rendered[item.relative_path] = item
    return rendered


def broker_snapshot(tenants: Iterable[dict[str, Any]], generation: int) -> str:
    result: dict[str, Any] = {"schema_version": 1, "generation": int(generation), "tenants": {}}
    for tenant in sorted(tenants, key=lambda item: str(item["id"])):
        cfg = _normalized(tenant)
        result["tenants"][cfg["id"]] = {
            "public_hosts": cfg["hosts"], "origin": cfg["origin"],
            "load_balancers": cfg["lb"], "vod_hosts": cfg["vod"], "ttl_seconds": 15,
            "vod_policy": {
                "seeds": [{"host": item["host"],
                           "schemes": ["https"] if item["port"] == 443 else ["http"],
                           "ports": [item["port"]]} for item in cfg["vod"]],
                "allow_chain_derived_hosts": True,
                "derived_host_ports": [80, 443],
                "max_redirects": 5,
            },
        }
    return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
