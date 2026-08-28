#!/usr/bin/env python3
"""Menu whiptail unificado: legado e plano de controle multi-edge."""
from __future__ import annotations

import gc
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database, normalize_id
from core.deploy import queue_deployment
from core.edge_manager import bootstrap_edge, scan_host_identity
from core.render_tenants import render_tenant

DB_PATH = os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db")
LEGACY_MENU = "/usr/local/bin/mago-cdn-legacy"


def dialog(args: list[str]) -> tuple[int, str]:
    read_fd, write_fd = os.pipe()
    command = ["whiptail", "--title", "Mago CDN — Multi-Edge", "--output-fd", str(write_fd), *args]
    try:
        result = subprocess.run(command, pass_fds=(write_fd,), check=False)
        os.close(write_fd)
        output = os.read(read_fd, 1024 * 1024).decode(errors="replace").strip()
    finally:
        try:
            os.close(read_fd)
        except OSError:
            return 1, ""
    return result.returncode, output


def choose(title: str, entries: list[tuple[str, str]], height: int = 20) -> str | None:
    flat = [part for entry in entries for part in entry]
    code, value = dialog(["--menu", title, str(height), "92", str(min(len(entries), 12)), *flat])
    return value if code == 0 else None


def ask(label: str, default: str = "", password: bool = False) -> str | None:
    kind = "--passwordbox" if password else "--inputbox"
    code, value = dialog([kind, label, "10", "88", default])
    return value if code == 0 else None


def confirm(text: str) -> bool:
    code, _ = dialog(["--yesno", text, "12", "88"])
    return code == 0


def message(text: str, height: int = 20) -> None:
    dialog(["--scrolltext", "--msgbox", text or "Sem dados.", str(height), "92"])


def service_state(name: str) -> str:
    result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, check=False)
    return result.stdout.strip() or "desconhecido"


def dashboard(db: Database) -> None:
    edges = db.edges(); tenants = db.tenants(); dns = db.dns_records()
    deployments = db.rows("SELECT id,state,release_id FROM deployments ORDER BY created_at DESC LIMIT 5")
    lines = [
        "PLANO DE CONTROLE",
        f"Edges: {len(edges)} (ready: {sum(x['state'] == 'ready' for x in edges)})",
        f"Tenants: {len(tenants)}",
        f"Registros DNS ativos: {sum(x['status'] == 'active' for x in dns)}",
        f"Painel web: {service_state('cdnmnus-admin.service')}",
        f"Orquestrador: {service_state('cdnmnus-orchestrator.service')}",
        f"Nginx: {service_state('nginx.service')}",
        "",
        "DEPLOYMENTS RECENTES",
    ]
    lines.extend(f"{x['id']}  {x['state']}  {x['release_id']}" for x in deployments)
    if not deployments: lines.append("Nenhum deployment.")
    message("\n".join(lines))


def edge_list(db: Database) -> None:
    edges = db.edges()
    lines = [f"{x['id']} | {x['name']} | {x['ipv4']}:{x['ssh_port']} | {x['state']} | {x['deployed_version'] or '-'}" for x in edges]
    message("EDGES\n\n" + ("\n".join(lines) if lines else "Nenhuma edge cadastrada."))


def edge_add(db: Database) -> None:
    name = ask("Nome da edge")
    if name is None: return
    edge_id_raw = ask("ID técnico [a-z0-9_-]", name.lower().replace(" ", "-"))
    if edge_id_raw is None: return
    edge_id = normalize_id(edge_id_raw, "edge_id")
    ipv4 = ask("IPv4 público da edge")
    if ipv4 is None: return
    port_raw = ask("Porta SSH", "22")
    if port_raw is None: return
    initial_user = ask("Usuário SSH inicial", "root")
    if initial_user is None: return
    identity = scan_host_identity(ipv4, int(port_raw))
    if not confirm(
        "FINGERPRINT SSH APRESENTADO\n\n"
        f"Algoritmo: {identity.key_type}\n{identity.sha256}\n\n"
        "Compare obrigatoriamente com o console do provedor. Está correto?"
    ):
        raise PermissionError("fingerprint não confirmado")
    typed = ask("Digite o fingerprint completo para confirmar")
    if typed != identity.sha256:
        raise PermissionError("fingerprint digitado diverge")
    password = ask("Senha inicial (não será armazenada)", password=True)
    if password is None: return
    try:
        result = bootstrap_edge(ipv4, int(port_raw), initial_user, password, typed, edge_id)
    finally:
        password = ""; del password; gc.collect()
    db.add_edge(edge_id, name, ipv4, int(port_raw), result["ssh_user"], result["fingerprint"], "bootstrapping")
    message(f"Edge {edge_id} cadastrada em bootstrapping.\nConexão por chave Ed25519 validada; ainda não publicada no DNS.")


def edge_action(db: Database, state: str) -> None:
    edges = db.edges()
    selected = choose("Selecionar edge", [(x["id"], f"{x['name']} — {x['state']}") for x in edges])
    if selected is None: return
    if confirm(f"Alterar {selected} para {state}?"):
        db.set_edge_state(selected, state); db.sync_dns_matrix()
        message(f"Edge {selected}: {state}. Matriz DNS recalculada.")


def edges_menu(db: Database) -> None:
    while True:
        action = choose("Gerenciar Edges", [
            ("list", "Listar edges e versões"), ("add", "Adicionar e executar bootstrap seguro"),
            ("drain", "Drenar edge do pool local"), ("ready", "Marcar edge como ready"),
            ("disable", "Desabilitar edge"), ("back", "Voltar"),
        ])
        try:
            if action in (None, "back"): return
            if action == "list": edge_list(db)
            elif action == "add": edge_add(db)
            elif action == "drain": edge_action(db, "draining")
            elif action == "ready": edge_action(db, "ready")
            elif action == "disable": edge_action(db, "disabled")
        except Exception as exc: message("Falha na operação de edge:\n\n" + str(exc))


def tenant_list(db: Database) -> None:
    tenants = db.tenants()
    lines = []
    for item in tenants:
        lines.append(f"{item['id']} | {item['name']} | {item['canonical_host']} | v{item['config_version']}")
        lines.extend(f"  - {host['hostname']} | TLS {host['tls_status']}" for host in item["hosts"])
        lines.extend(f"  - {up['kind']}: {up['host']}:{up['port']}" for up in item["upstreams"])
    message("TENANTS / XUIs\n\n" + ("\n".join(lines) if lines else "Nenhum tenant cadastrado."))


def tenant_add(db: Database) -> None:
    tenant_id = ask("ID do tenant [a-z0-9_-]")
    if tenant_id is None: return
    name = ask("Nome do XUI/Tenant")
    if name is None: return
    canonical = ask("Hostname canônico público")
    if canonical is None: return
    origin = ask("IP ou hostname da origem")
    if origin is None: return
    port = ask("Porta da origem", "80")
    if port is None: return
    lbs = ask("Load balancers separados por vírgula", "")
    if lbs is None: return
    db.add_tenant(tenant_id, name, canonical, origin, int(port), [x.strip() for x in lbs.split(",") if x.strip()])
    message(f"Tenant {tenant_id} cadastrado.")


def tenant_cname(db: Database) -> None:
    tenants = db.tenants()
    selected = choose("Tenant do novo CNAME", [(x["id"], x["canonical_host"]) for x in tenants])
    if selected is None: return
    hostname = ask("Hostname/alias do cliente")
    if hostname is None: return
    db.add_cname(selected, hostname); message(f"Alias {hostname} associado a {selected}. TLS pendente.")


def tenant_vhost(db: Database) -> None:
    tenants = db.tenants()
    selected = choose("Visualizar vhost", [(x["id"], x["canonical_host"]) for x in tenants])
    if selected is None: return
    message(render_tenant(db.tenant(selected)).content, 28)


def tenants_menu(db: Database) -> None:
    while True:
        action = choose("Gerenciar XUIs / Tenants", [
            ("list", "Listar tenants, hosts e upstreams"), ("add", "Cadastrar novo XUI/Tenant"),
            ("cname", "Adicionar domínio/CNAME"), ("vhost", "Visualizar vhost Nginx gerado"),
            ("back", "Voltar"),
        ])
        try:
            if action in (None, "back"): return
            if action == "list": tenant_list(db)
            elif action == "add": tenant_add(db)
            elif action == "cname": tenant_cname(db)
            elif action == "vhost": tenant_vhost(db)
        except Exception as exc: message("Falha na operação de tenant:\n\n" + str(exc))


def dns_menu(db: Database) -> None:
    try:
        matrix = db.sync_dns_matrix()
        lines = [f"{x['hostname']} -> [{', '.join(x['targets']) or 'sem edge ready'}] | TLS {x['tls_status']}" for x in matrix]
        message("MATRIZ DNS RECALCULADA\n\n" + ("\n".join(lines) if lines else "Nenhum hostname habilitado."))
    except Exception as exc: message("Falha no DNS:\n\n" + str(exc))


def deployments_menu(db: Database) -> None:
    while True:
        action = choose("Deployments", [
            ("list", "Listar deployments recentes"), ("queue", "Compilar release e enfileirar deploy serial"),
            ("back", "Voltar"),
        ])
        try:
            if action in (None, "back"): return
            if action == "list":
                rows = db.rows("SELECT id,state,release_id,error,created_at FROM deployments ORDER BY created_at DESC LIMIT 30")
                message("\n".join(f"{x['created_at']} | {x['state']} | {x['release_id']}\n{x['id']}\n{x['error'] or ''}" for x in rows) or "Nenhum deployment.", 26)
            elif action == "queue" and confirm("Compilar a configuração atual e enfileirar rollout serial?"):
                result = queue_deployment(db)
                message(f"Deployment enfileirado:\n{result['deployment_id']}\nRelease: {result['release_id']}")
        except Exception as exc: message("Falha no deployment:\n\n" + str(exc))


def services_menu() -> None:
    while True:
        action = choose("Serviços", [
            ("status", "Ver status resumido"), ("restart-web", "Reiniciar painel administrativo"),
            ("restart-worker", "Reiniciar orquestrador"), ("reload-nginx", "Validar e recarregar Nginx"),
            ("back", "Voltar"),
        ])
        if action in (None, "back"): return
        if action == "status":
            message("\n".join(f"{name}: {service_state(name)}" for name in ("cdnmnus-admin.service", "cdnmnus-orchestrator.service", "cdnmnus-panel.service", "cdnmnus-token-broker.service", "nginx.service")))
        elif action == "restart-web": subprocess.run(["systemctl", "restart", "cdnmnus-admin.service"], check=False); message("Painel reiniciado.")
        elif action == "restart-worker": subprocess.run(["systemctl", "restart", "cdnmnus-orchestrator.service"], check=False); message("Orquestrador reiniciado.")
        elif action == "reload-nginx":
            test = subprocess.run(["nginx", "-t"], capture_output=True, text=True, check=False)
            if test.returncode == 0:
                subprocess.run(["systemctl", "reload", "nginx"], check=True); message("Nginx validado e recarregado.")
            else: message("nginx -t falhou; reload cancelado.\n\n" + test.stderr)


def access_menu(db: Database) -> None:
    current = int(db.setting("web_port", 8080))
    action = choose("Painel Web", [
        ("instructions", "Exibir comando de túnel SSH"), ("port", f"Alterar porta persistida (atual: {current})"),
        ("back", "Voltar"),
    ])
    if action == "instructions":
        message(f"No computador local, execute:\n\nssh -L {current}:127.0.0.1:{current} root@IP_DO_SERVIDOR\n\nDepois abra:\nhttp://127.0.0.1:{current}\n\nUsuário: admin")
    elif action == "port":
        raw = ask("Nova porta HTTP", str(current))
        if raw is None: return
        port = int(raw)
        if not 1 <= port <= 65535: raise ValueError("porta inválida")
        db.set_setting("web_port", port)
        message("Porta salva no SQLite.\n\nSe CDNMNUS_ADMIN_PORT estiver em admin.env, ele tem precedência. Reinicie o painel após ajustar o env.")


def main() -> int:
    if os.geteuid() != 0:
        print("Execute como root via SSH.", file=sys.stderr); return 1
    if shutil.which("whiptail") is None:
        print("whiptail não está instalado.", file=sys.stderr); return 1
    db = Database(DB_PATH); db.initialize()
    while True:
        action = choose("Menu Principal", [
            ("dashboard", "Dashboard multi-edge"), ("edges", "Gerenciar Edges"),
            ("tenants", "Gerenciar XUIs / Tenants / CNAMEs"), ("dns", "DNS e Failover"),
            ("deploy", "Deployments e rollout serial"), ("services", "Serviços e Nginx"),
            ("web", "Acesso e porta do painel web"), ("legacy", "Painel legado de perfil XUI ativo"),
            ("exit", "Sair"),
        ], 24)
        try:
            if action in (None, "exit"): return 0
            if action == "dashboard": dashboard(db)
            elif action == "edges": edges_menu(db)
            elif action == "tenants": tenants_menu(db)
            elif action == "dns": dns_menu(db)
            elif action == "deploy": deployments_menu(db)
            elif action == "services": services_menu()
            elif action == "web": access_menu(db)
            elif action == "legacy":
                if Path(LEGACY_MENU).is_file(): subprocess.run([LEGACY_MENU], check=False)
                else: message("Backup do menu legado não encontrado.")
        except Exception as exc:
            message("Operação falhou:\n\n" + str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
