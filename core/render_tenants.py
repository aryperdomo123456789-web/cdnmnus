"""Renderização pura e determinística dos vhosts Nginx por tenant.

``health_host`` pertence ao tenant e deve ser um dos seus hosts publicados.
Por padrão é o canonical; isso evita que probes usem um hostname global que
caia no vhost default e receba 421. O renderer não cria certificados nem
altera DNS: a cobertura SAN é um gate separado do provisionamento.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from core.db import normalize_hostname, normalize_id, normalize_origin_host, normalize_port


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
    health_host = normalize_hostname(str(tenant.get("health_host") or canonical))
    if health_host not in aliases:
        raise ValueError(f"health_host do tenant {tenant_id} não está cadastrado")
    playlist_host = normalize_hostname(str(tenant.get("playlist_host") or canonical))
    if playlist_host not in aliases:
        raise ValueError(f"host de playlist do tenant {tenant_id} não está cadastrado")
    upstreams = tenant.get("upstreams", [])
    origin = [item for item in upstreams if item["kind"] == "origin"]
    if len(origin) != 1:
        raise ValueError(f"tenant {tenant_id} deve ter exatamente uma origem")
    grouped = {kind: [] for kind in ("lb", "vod")}
    for item in upstreams:
        if item["kind"] in grouped:
            grouped[item["kind"]].append({"host": normalize_origin_host(str(item["host"])), "port": normalize_port(item["port"])})
    return {"id": tenant_id, "hosts": aliases, "health_host": health_host, "playlist_host": playlist_host,
            "origin": {"host": normalize_hostname(str(origin[0]["host"])), "port": normalize_port(origin[0]["port"])},
            "lb": sorted(grouped["lb"], key=lambda x: (x["host"], x["port"])),
            "vod": sorted(grouped["vod"], key=lambda x: (x["host"], x["port"]))}


def render_tenant(tenant: dict[str, Any]) -> RenderedTenant:
    cfg = _normalized(tenant)
    tid = cfg["id"]
    origin = cfg["origin"]
    canonical = cfg["hosts"][0]
    health_host = cfg["health_host"]
    server_names = " ".join(_nginx(host) for host in cfg["hosts"])
    admin_fail_closed = '''    # Rotas administrativas nunca chegam ao XUI.
    location ~* ^/(?:admin|administrator|phpmyadmin|pma|mysql|database|internal)(?:/|$) {
        access_log off;
        return 421;
    }
'''
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
    # Alguns aplicativos aceitam somente HTTP. Mantemos as mesmas rotas
    # protegidas do vhost TLS, sempre ocultando a origem nos manifestos.
    location = /get.php {{
        proxy_pass http://origin_{tid};
        proxy_set_header Host {server_names.split()[0]};
        proxy_set_header Accept-Encoding "";
        proxy_hide_header Location;
        proxy_hide_header Server;
        sub_filter_types *;
        sub_filter_once off;
        sub_filter 'http://{_nginx(origin['host'])}:{origin['port']}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'http://{_nginx(origin['host'])}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'https://{_nginx(origin['host'])}:{origin['port']}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'https://{_nginx(origin['host'])}' 'http://{_nginx(cfg['playlist_host'])}';
    }}
    location = /player_api.php {{
        proxy_pass http://origin_{tid};
        proxy_set_header Host {server_names.split()[0]};
        proxy_set_header Accept-Encoding "";
        proxy_hide_header Location;
        proxy_hide_header Server;
        proxy_hide_header Set-Cookie;
        proxy_hide_header Via;
        proxy_hide_header X-Powered-By;
        sub_filter_types application/json text/plain *;
        sub_filter_once off;
        sub_filter 'http://{_nginx(origin['host'])}:{origin['port']}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'http://{_nginx(origin['host'])}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'https://{_nginx(origin['host'])}:{origin['port']}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'https://{_nginx(origin['host'])}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter '{_nginx(origin['host'])}' '{_nginx(cfg['playlist_host'])}';
    }}
    location ~ ^/(?:hls|live)/ {{
        proxy_pass http://broker_{tid};
        proxy_set_header X-CDN-Tenant {tid};
        proxy_set_header X-CDN-Public-Host $host;
        proxy_set_header X-Broker-Action resolve;
        proxy_set_header X-Original-URI $request_uri;
        proxy_pass_request_body off;
    }}
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
        proxy_cache_key "{tid}|$request_method|$host|$uri";
        proxy_cache_bypass $http_range;
        proxy_no_cache $http_range;
        proxy_read_timeout 20s;
        proxy_hide_header Location;
        proxy_hide_header Server;
    }}

{lb_locations}
{admin_fail_closed}    location / {{ return 421; }}
}}

server {{
    listen 443 ssl;
    server_name {server_names};
    ssl_certificate /etc/letsencrypt/live/{_nginx(cfg['hosts'][0])}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{_nginx(cfg['hosts'][0])}/privkey.pem;

    location = /edge-health {{
        proxy_pass http://broker_{tid}/health;
        proxy_set_header Host {health_host};
        proxy_set_header X-CDN-Public-Host {health_host};
        proxy_pass_request_body off;
        access_log off;
    }}

    # XUI get.php returns a playlist body. Rewrite origin authorities at the
    # edge so clients receive only the public hostname, never the XUI IP.
    location = /get.php {{
        proxy_pass http://origin_{tid};
        proxy_set_header Host {server_names.split()[0]};
        proxy_set_header Accept-Encoding "";
        proxy_hide_header Location;
        proxy_hide_header Server;
        sub_filter_types *;
        sub_filter_once off;
        sub_filter 'http://{_nginx(origin['host'])}:{origin['port']}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'http://{_nginx(origin['host'])}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'https://{_nginx(origin['host'])}:{origin['port']}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'https://{_nginx(origin['host'])}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'http://{_nginx(canonical)}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'https://{_nginx(canonical)}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter '{_nginx(origin['host'])}' '{_nginx(cfg['playlist_host'])}';
    }}
    location = /player_api.php {{
        proxy_pass http://origin_{tid};
        proxy_set_header Host {server_names.split()[0]};
        proxy_set_header Accept-Encoding "";
        proxy_hide_header Location;
        proxy_hide_header Server;
        proxy_hide_header Set-Cookie;
        proxy_hide_header Via;
        proxy_hide_header X-Powered-By;
        sub_filter_types application/json text/plain *;
        sub_filter_once off;
        sub_filter 'http://{_nginx(origin['host'])}:{origin['port']}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'http://{_nginx(origin['host'])}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'https://{_nginx(origin['host'])}:{origin['port']}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'https://{_nginx(origin['host'])}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'http://{_nginx(canonical)}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter 'https://{_nginx(canonical)}' 'http://{_nginx(cfg['playlist_host'])}';
        sub_filter '{_nginx(origin['host'])}' '{_nginx(cfg['playlist_host'])}';
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

{admin_fail_closed}    location = / {{
        default_type text/html;
        add_header Cache-Control "no-store" always;
        return 200 "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='robots' content='noindex'><meta name='color-scheme' content='dark'><title>Mago Edge Infrastructure</title><style>body{{margin:0;background:#080d18;color:#eef4ff;font:16px system-ui,sans-serif;display:grid;place-items:center;min-height:100vh}}main{{max-width:760px;width:calc(100% - 48px);padding:48px;min-height:55vh;display:flex;flex-direction:column;justify-content:center}}h1{{font-size:clamp(36px,6vw,64px);line-height:1.05;margin:0 0 18px;letter-spacing:-.03em}}p{{color:#94a3b8;line-height:1.6}}.ok{{color:#22c55e;font-weight:600;letter-spacing:.02em}}footer{{margin-top:56px;padding-top:18px;border-top:1px solid #223047;color:#64748b;font-size:13px;letter-spacing:.02em}}</style></head><body><main><p class='ok'>● CDN ACTIVE.</p><h1>Content delivery at the edge.</h1><p>Protected edge network. Direct access is restricted by edge security policies.</p><footer>2026 @MagoPD. All rights reserved.</footer></main></body></html>";
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


def broker_snapshot(tenants: Iterable[dict[str, Any]], generation: int,
                    *, automatic_cname_discovery: bool = False) -> str:
    result: dict[str, Any] = {
        "schema_version": 1,
        "generation": int(generation),
        "automatic_cname_discovery": bool(automatic_cname_discovery),
        "tenants": {},
    }
    for tenant in sorted(tenants, key=lambda item: str(item["id"])):
        cfg = _normalized(tenant)
        result["tenants"][cfg["id"]] = {
        "public_hosts": cfg["hosts"], "origin": cfg["origin"],
            "health_host": cfg["health_host"], "load_balancers": cfg["lb"], "vod_hosts": cfg["vod"], "ttl_seconds": 15,
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
