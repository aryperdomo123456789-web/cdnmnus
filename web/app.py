#!/usr/bin/env python3
"""Servidor HTTP administrativo sem dependências de framework."""
from __future__ import annotations

import argparse
import base64
import gc
import hashlib
import hmac
import json
import os
import secrets
import ssl
import sys
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database, normalize_id
from core.deploy import queue_deployment
from core.edge_manager import bootstrap_edge, scan_host_identity
from core.render_tenants import render_tenant

MAX_BODY = 64 * 1024
DB = Database()
CSRF_TOKEN = secrets.token_urlsafe(32)
ADMIN_USER = os.environ.get("CDNMNUS_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("CDNMNUS_ADMIN_PASSWORD", "")


HTML = r'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf" content="__CSRF__"><title>cdnmnus Control Plane</title><style>
:root{color-scheme:dark;--bg:#08111f;--panel:#101c2d;--line:#26364d;--text:#eef4ff;--muted:#91a4bd;--accent:#3b82f6;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif}.layout{display:grid;grid-template-columns:235px 1fr;min-height:100vh}aside{border-right:1px solid var(--line);padding:24px 14px;background:#0a1423}h1{font-size:20px;margin:0 8px 28px}.nav{display:grid;gap:7px}.nav button{background:transparent;color:var(--muted);border:0;padding:12px;text-align:left;border-radius:8px;cursor:pointer}.nav button.active{background:#17263b;color:white}main{padding:30px;max-width:1250px;width:100%}.view{display:none}.view.active{display:block}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:16px}.stat strong{display:block;font-size:28px}.muted,.stat span{color:var(--muted)}label{display:block;margin:11px 0 5px;color:#c4d2e5}input,textarea,select{width:100%;padding:10px;background:#091525;border:1px solid var(--line);border-radius:7px;color:white}textarea{min-height:90px}button{background:var(--accent);border:0;border-radius:7px;color:white;padding:9px 13px;cursor:pointer}.danger{background:#7f1d1d}.secondary{background:#334155}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid var(--line);padding:10px 7px}th{color:var(--muted)}.status{font-weight:700}.ready,.active,.valid{color:var(--ok)}.failed{color:var(--bad)}.draining,.pending{color:var(--warn)}pre{white-space:pre-wrap;overflow:auto;background:#07101d;padding:14px;border-radius:8px;max-height:430px}.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}.notice{position:fixed;right:20px;bottom:20px;background:#17263b;border:1px solid var(--line);padding:13px 16px;border-radius:8px;display:none;max-width:420px}@media(max-width:850px){.layout{display:block}aside{position:static}.nav{display:flex;overflow:auto}main{padding:18px}.grid,.two{grid-template-columns:1fr}}
</style></head><body><div class="layout"><aside><h1>cdnmnus<br><span class="muted">Control Plane</span></h1><div class="nav">
<button class="active" data-view="dashboard">Dashboard</button><button data-view="edges">Gerenciar Edges</button><button data-view="tenants">Gerenciar XUIs</button><button data-view="domains">Domínios & CNAMEs</button><button data-view="dns">DNS & Failover</button><button data-view="settings">Configuração</button></div></aside><main>
<section id="dashboard" class="view active"><h2>Dashboard</h2><div class="grid"><div class="card stat"><strong id="nEdges">0</strong><span>Edges ready</span></div><div class="card stat"><strong id="nTenants">0</strong><span>Tenants</span></div><div class="card stat"><strong id="nDns">0</strong><span>Registros ativos</span></div></div><div class="card"><h3>Saúde das edges</h3><div id="health"></div></div></section>
<section id="edges" class="view"><h2>Gerenciar Edges</h2><div class="two"><form class="card" id="edgeForm"><h3>Bootstrap seguro</h3><label>ID</label><input name="id" required pattern="[a-z0-9_-]+"><label>Nome</label><input name="name" required><label>IPv4 público</label><input name="ipv4" required><label>Porta SSH</label><input name="ssh_port" type="number" value="22" required><label>Usuário inicial</label><input name="ssh_user" value="root" required><label>Senha inicial (somente memória)</label><input name="password" type="password" autocomplete="new-password" required><label>Fingerprint confirmado</label><input name="fingerprint" readonly required><div class="actions"><button type="button" id="scanKey">Ler fingerprint</button><button type="submit">Confirmar e cadastrar</button></div></form><div class="card"><h3>Edges</h3><div id="edgeList"></div></div></div></section>
<section id="tenants" class="view"><h2>Gerenciar XUIs</h2><div class="two"><form class="card" id="tenantForm"><label>ID</label><input name="id" required><label>Nome</label><input name="name" required><label>Host canônico</label><input name="canonical_host" required><label>Origem</label><input name="origin_host" required><label>Porta</label><input name="origin_port" type="number" value="80" required><label>Load balancers (um por linha)</label><textarea name="load_balancers"></textarea><button type="submit">Cadastrar tenant</button></form><div class="card"><h3>Tenants e vhosts</h3><div id="tenantList"></div><pre id="vhost">Selecione “Ver vhost”.</pre></div></div></section>
<section id="domains" class="view"><h2>Domínios & CNAMEs</h2><form class="card" id="cnameForm"><label>Tenant</label><select name="tenant_id" id="tenantSelect"></select><label>Alias do cliente</label><input name="hostname" required><button type="submit">Adicionar CNAME</button></form><div class="card" id="domainList"></div></section>
<section id="dns" class="view"><h2>DNS & Failover</h2><div class="card"><p class="muted">A matriz inclui somente edges em estado ready. A sincronização externa é opcional e local.</p><button id="dnsSync">Recalcular matriz</button><button id="deploy" class="secondary">Deploy serial</button><div id="dnsList"></div></div></section>
<section id="settings" class="view"><h2>Configuração local</h2><form class="card" id="portForm"><label>Porta HTTP do painel</label><input name="port" id="webPort" type="number" min="1" max="65535" required><p class="muted">A alteração é salva no SQLite. Reinicie o painel para que o socket use a nova porta.</p><button type="submit">Salvar porta</button></form></section>
</main></div><div class="notice" id="notice"></div><script>
const csrf=document.querySelector('meta[name=csrf]').content,$=s=>document.querySelector(s);let data={};
const notify=(m,bad=false)=>{let n=$('#notice');n.textContent=m;n.style.display='block';n.style.borderColor=bad?'#7f1d1d':'#166534';setTimeout(()=>n.style.display='none',5000)};
const api=async(path,options={})=>{options.headers={...(options.headers||{}),'X-CSRF-Token':csrf};if(options.body)options.headers['Content-Type']='application/json';let r=await fetch(path,options),j=await r.json();if(!r.ok)throw Error(j.error||`HTTP ${r.status}`);return j};
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-view]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.view').forEach(x=>x.classList.toggle('active',x.id===b.dataset.view))});
const rows=(headers,items)=>`<table><thead><tr>${headers.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${items.join('')}</tbody></table>`;
async function refresh(){data=await api('/api/state');$('#nEdges').textContent=data.edges.filter(x=>x.state==='ready').length;$('#nTenants').textContent=data.tenants.length;$('#nDns').textContent=data.dns_records.filter(x=>x.status==='active').length;$('#webPort').value=data.web_port;$('#health').innerHTML=rows(['Edge','IP','Estado','Ações'],data.edges.map(x=>`<tr><td>${x.name}</td><td>${x.ipv4}</td><td class="status ${x.state}">${x.state}</td><td><button onclick="health('${x.id}')">Testar</button></td></tr>`));$('#edgeList').innerHTML=rows(['Nome','IP','Status',''],data.edges.map(x=>`<tr><td>${x.name}</td><td>${x.ipv4}</td><td class="${x.state}">${x.state}</td><td><button class="danger" onclick="drain('${x.id}')">Drenar</button></td></tr>`));$('#tenantList').innerHTML=rows(['ID','Host','Versão',''],data.tenants.map(x=>`<tr><td>${x.id}</td><td>${x.canonical_host}</td><td>${x.config_version}</td><td><button onclick="vhost('${x.id}')">Ver vhost</button></td></tr>`));$('#tenantSelect').innerHTML=data.tenants.map(x=>`<option value="${x.id}">${x.id}</option>`).join('');$('#domainList').innerHTML=rows(['Hostname','Tenant','SSL'],data.tenants.flatMap(x=>x.hosts.map(h=>`<tr><td>${h.hostname}</td><td>${x.id}</td><td class="${h.tls_status}">${h.tls_status}</td></tr>`)));$('#dnsList').innerHTML=rows(['Hostname','Tipo','Destino','Status'],data.dns_records.map(x=>`<tr><td>${x.hostname}</td><td>${x.record_type}</td><td>${x.target_ip}</td><td class="${x.status}">${x.status}</td></tr>`))}
window.health=async id=>{try{let x=await api(`/api/edges/${id}/health`,{method:'POST',body:'{}'});notify(`Health ${x.status}`);await refresh()}catch(e){notify(e.message,true)}};window.drain=async id=>{if(!confirm('Drenar esta edge do pool local?'))return;try{await api(`/api/edges/${id}/drain`,{method:'POST',body:'{}'});await refresh()}catch(e){notify(e.message,true)}};window.vhost=async id=>{try{$('#vhost').textContent=(await api(`/api/tenants/${id}/vhost`)).content}catch(e){notify(e.message,true)}};
$('#scanKey').onclick=async()=>{let f=new FormData($('#edgeForm'));try{let x=await api('/api/edges/scan',{method:'POST',body:JSON.stringify({ipv4:f.get('ipv4'),ssh_port:Number(f.get('ssh_port'))})});$('#edgeForm').fingerprint.value=x.fingerprint;notify('Confira o fingerprint no console do provedor antes de cadastrar.')}catch(e){notify(e.message,true)}};
$('#edgeForm').onsubmit=async e=>{e.preventDefault();let f=Object.fromEntries(new FormData(e.target));f.ssh_port=Number(f.ssh_port);try{await api('/api/edges',{method:'POST',body:JSON.stringify(f)});e.target.reset();e.target.ssh_port.value=22;e.target.ssh_user.value='root';await refresh();notify('Edge cadastrada e conexão por chave validada.')}catch(x){notify(x.message,true)}finally{e.target.password.value=''}};
$('#tenantForm').onsubmit=async e=>{e.preventDefault();let f=Object.fromEntries(new FormData(e.target));f.origin_port=Number(f.origin_port);f.load_balancers=f.load_balancers.split(/\n|,/).map(x=>x.trim()).filter(Boolean);try{await api('/api/tenants',{method:'POST',body:JSON.stringify(f)});e.target.reset();e.target.origin_port.value=80;await refresh();notify('Tenant cadastrado.')}catch(x){notify(x.message,true)}};
$('#cnameForm').onsubmit=async e=>{e.preventDefault();let f=Object.fromEntries(new FormData(e.target));try{await api('/api/cnames',{method:'POST',body:JSON.stringify(f)});e.target.reset();await refresh();notify('CNAME associado.')}catch(x){notify(x.message,true)}};
$('#dnsSync').onclick=async()=>{try{await api('/api/dns/sync',{method:'POST',body:'{}'});await refresh();notify('Matriz DNS recalculada.')}catch(e){notify(e.message,true)}};$('#deploy').onclick=async()=>{if(!confirm('Enfileirar rollout serial nas edges ready?'))return;try{let x=await api('/api/deploy',{method:'POST',body:'{}'});notify(`Deployment ${x.deployment_id} enfileirado`)}catch(e){notify(e.message,true)}};
$('#portForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/settings/port',{method:'POST',body:JSON.stringify({port:Number(e.target.port.value)})});notify('Porta salva. Reinicie o painel para aplicar.')}catch(x){notify(x.message,true)}};refresh().catch(e=>notify(e.message,true));setInterval(()=>refresh().catch(()=>{}),30000);
</script></body></html>'''


def _auth_ok(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        user, password = base64.b64decode(header[6:], validate=True).decode().split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(user, ADMIN_USER) and hmac.compare_digest(password, ADMIN_PASSWORD)


class Handler(BaseHTTPRequestHandler):
    server_version = "cdnmnus-admin"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"cdnmnus-admin: {self.command} {self.path.split('?', 1)[0]} - {fmt % args}")

    def _auth(self) -> bool:
        if _auth_ok(self.headers.get("Authorization", "")):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="cdnmnus-control-plane"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _payload(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type deve ser application/json")
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), CSRF_TOKEN):
            raise PermissionError("token CSRF inválido")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("corpo inválido")
        return json.loads(self.rfile.read(length).decode())

    def do_GET(self) -> None:
        if not self._auth(): return
        route = urlsplit(self.path).path
        try:
            if route == "/":
                body = HTML.replace("__CSRF__", CSRF_TOKEN).encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store"); self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if route == "/api/state":
                self._json(200, {"edges": DB.edges(), "tenants": DB.tenants(), "dns_records": DB.dns_records(),
                                 "deployments": DB.rows("SELECT * FROM deployments ORDER BY created_at DESC LIMIT 20"),
                                 "web_port": DB.setting("web_port", 8080)}); return
            parts = route.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "tenants"] and parts[3] == "vhost":
                self._json(200, {"content": render_tenant(DB.tenant(parts[2])).content}); return
            self._json(404, {"error": "rota não encontrada"})
        except Exception as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self._auth(): return
        route = urlsplit(self.path).path
        password = ""
        try:
            payload = self._payload()
            if route == "/api/edges/scan":
                identity = scan_host_identity(str(payload.get("ipv4", "")), int(payload.get("ssh_port", 22)))
                self._json(200, {"fingerprint": identity.sha256, "key_type": identity.key_type}); return
            if route == "/api/edges":
                password = str(payload.pop("password", ""))
                edge_id = normalize_id(str(payload.get("id", "")), "edge_id")
                result = bootstrap_edge(str(payload.get("ipv4", "")), int(payload.get("ssh_port", 22)),
                                        str(payload.get("ssh_user", "root")), password,
                                        str(payload.get("fingerprint", "")), edge_id)
                edge = DB.add_edge(edge_id, str(payload.get("name", "")), str(payload.get("ipv4", "")),
                                   int(payload.get("ssh_port", 22)), result["ssh_user"], result["fingerprint"], "bootstrapping")
                self._json(201, {"edge": edge}); return
            if route == "/api/tenants":
                tenant = DB.add_tenant(str(payload.get("id", "")), str(payload.get("name", "")),
                                       str(payload.get("canonical_host", "")), str(payload.get("origin_host", "")),
                                       int(payload.get("origin_port", 80)), payload.get("load_balancers", []))
                self._json(201, {"tenant": tenant}); return
            if route == "/api/cnames":
                self._json(201, DB.add_cname(str(payload.get("tenant_id", "")), str(payload.get("hostname", "")))); return
            if route == "/api/dns/sync":
                self._json(200, {"matrix": DB.sync_dns_matrix()}); return
            if route == "/api/deploy":
                self._json(202, queue_deployment(DB)); return
            if route == "/api/settings/port":
                port = int(payload.get("port", 0))
                if not 1 <= port <= 65535: raise ValueError("porta inválida")
                DB.set_setting("web_port", port); self._json(200, {"web_port": port, "restart_required": True}); return
            parts = route.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "edges"] and parts[3] == "drain":
                DB.set_edge_state(parts[2], "draining"); DB.sync_dns_matrix(); self._json(200, {"state": "draining"}); return
            if len(parts) == 4 and parts[:2] == ["api", "edges"] and parts[3] == "health":
                edge = DB.edge(parts[2]); context = ssl._create_unverified_context()
                tenants = DB.tenants(enabled_only=True)
                if not tenants:
                    raise ValueError("nenhum tenant habilitado para health check")
                health_host = tenants[0]["canonical_host"]
                request = urllib.request.Request(
                    f"https://{edge['ipv4']}/edge-health",
                    headers={"Host": health_host, "User-Agent": "cdnmnus-health-controller/1.0"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=5, context=context) as response: status = response.status
                except Exception: status = 503
                state = "ready" if status == 200 else "failed"; DB.set_edge_state(edge["id"], state); DB.sync_dns_matrix()
                self._json(200 if status == 200 else 503, {"status": status, "state": state}); return
            self._json(404, {"error": "rota não encontrada"})
        except PermissionError as exc:
            self._json(403, {"error": str(exc)})
        except Exception as exc:
            self._json(400, {"error": str(exc)})
        finally:
            password = ""; del password; gc.collect()


def main() -> None:
    global DB
    parser = argparse.ArgumentParser(description="Painel administrativo local cdnmnus")
    parser.add_argument("--db", default=os.environ.get("CDNMNUS_ADMIN_DB", "/etc/cdnmnus/admin.db"))
    parser.add_argument("--bind", default=os.environ.get("CDNMNUS_ADMIN_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--tls-cert", default=os.environ.get("CDNMNUS_ADMIN_TLS_CERT", ""))
    parser.add_argument("--tls-key", default=os.environ.get("CDNMNUS_ADMIN_TLS_KEY", ""))
    args = parser.parse_args()
    if not ADMIN_PASSWORD:
        raise SystemExit("CDNMNUS_ADMIN_PASSWORD é obrigatória")
    DB = Database(args.db); DB.initialize()
    port = args.port or int(os.environ.get("CDNMNUS_ADMIN_PORT", DB.setting("web_port", 8080)))
    if not 1 <= port <= 65535: raise SystemExit("porta HTTP inválida")
    server = ThreadingHTTPServer((args.bind, port), Handler)
    scheme = "http"
    if args.tls_cert or args.tls_key:
        if not args.tls_cert or not args.tls_key:
            raise SystemExit("TLS exige certificado e chave")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"cdnmnus-admin ouvindo em {scheme}://{args.bind}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
