#!/usr/bin/env python3
"""Menu whiptail unificado: legado e plano de controle multi-edge."""
from __future__ import annotations

import gc
import importlib.util
import json
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
from core.node_onboarding import onboard_node
from core.render_tenants import render_tenant
from core.topology import TopologyStore

DB_PATH = os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db")
LEGACY_PANEL = "/opt/cdnmnus-panel/panel.py"


def menu_title() -> str:
    try:
        node_id = Path("/etc/cdnmnus/node-id").read_text().strip()
        identity = json.loads(Path("/etc/cdnmnus/node-role.json").read_text())
        role = {"load_balancer": "LOAD BALANCER", "edge": "EDGE",
                "control_plane": "PLANO DE CONTROLE"}.get(identity.get("role"), identity.get("role", ""))
        return f"Mago CDN — Nó {node_id} — {role}"
    except Exception:
        return "Mago CDN — Central de Operações"


MENU_TITLE = menu_title()


def dialog(args: list[str]) -> tuple[int, str]:
    read_fd, write_fd = os.pipe()
    command = ["whiptail", "--title", MENU_TITLE, "--output-fd", str(write_fd), *args]
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


STATE_LABELS = {
    "pending": "pendente", "bootstrapping": "em preparação", "ready": "pronta",
    "draining": "em drenagem", "failed": "com falha", "disabled": "desabilitada",
    "queued": "na fila", "running": "em execução", "succeeded": "concluída",
    "rolled_back": "revertida",
}


def state_label(value: str) -> str:
    return STATE_LABELS.get(value, value)


def load_legacy_panel():
    """Carrega a implementação estável do perfil XUI sem abrir outro menu."""
    path = Path(LEGACY_PANEL)
    if not path.is_file():
        raise FileNotFoundError("módulo do perfil XUI atual não encontrado")
    spec = importlib.util.spec_from_file_location("mago_legacy_panel", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("não foi possível carregar o módulo do perfil XUI atual")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_profiles(panel) -> list[dict]:
    return panel.stored_profiles(panel.config_from_db())


def legacy_profile_status(panel, profile: dict) -> str:
    active = panel.config_from_db().get("active_profile_id", "")
    status = panel.profile_status(profile, active)
    balancers = profile.get("load_balancers", [])
    return (
        f"Nome: {profile.get('name', '')}\n"
        f"Origem XUI: {profile.get('upstream_host', '')}:{profile.get('upstream_port', 80)}\n"
        f"Domínio público: {profile.get('public_host', '')}\n"
        f"Balanceadores de origem: {len(balancers)}\n"
        f"Situação: {status}"
    )


def choose_legacy_profile(panel, title: str) -> dict | None:
    profiles = legacy_profiles(panel)
    if not profiles:
        message("Nenhum perfil XUI atual cadastrado.")
        return None
    selected = choose(title, [
        (item["profile_id"], item.get("name", "Perfil sem nome")) for item in profiles
    ])
    if selected is None:
        return None
    return next((item for item in profiles if item["profile_id"] == selected), None)


def edit_legacy_profile(panel, existing: dict | None = None) -> None:
    current = existing or {}
    name = ask("Nome do perfil XUI", current.get("name", ""))
    if name is None: return
    host = ask("IP ou DNS da origem XUI (HTTP, porta 80)", current.get("upstream_host", ""))
    if host is None: return
    public = ask("Domínio público desta VPS", current.get("public_host", ""))
    if public is None: return
    old_balancers = "\\n".join(current.get("load_balancers", []))
    balancers = ask("Balanceadores da origem (separe por \\n)", old_balancers)
    if balancers is None: return
    payload = {
        "profile_id": current.get("profile_id", ""),
        "name": name,
        "upstream_host": host,
        "upstream_port": 80,
        "public_host": public,
        "load_balancers": balancers.replace("\\n", "\n"),
    }
    saved = panel.save_profile(payload)
    message("Perfil XUI salvo e aplicado.\n\n" + legacy_profile_status(panel, saved))


def delete_legacy_profile(panel) -> None:
    profile = choose_legacy_profile(panel, "Excluir perfil XUI atual")
    if profile is None: return
    if not confirm(
        f"Excluir o perfil {profile.get('name', profile['profile_id'])}?\n\n"
        "Esta ação também remove seus balanceadores de origem e atualiza o proxy."
    ):
        return
    panel.delete_profile(profile["profile_id"])
    message("Perfil excluído e configuração do proxy atualizada.")


def current_xui_menu() -> None:
    """Fusão nativa das funções do antigo menu de perfil XUI."""
    panel = load_legacy_panel()
    while True:
        action = choose("Perfil XUI atual (compatibilidade)", [
            ("1", "Visão geral dos perfis atuais"),
            ("2", "Consultar perfil e balanceadores de origem"),
            ("3", "Cadastrar novo perfil XUI atual"),
            ("4", "Editar perfil XUI atual"),
            ("5", "Excluir perfil XUI atual"),
            ("6", "Validar e recarregar o proxy atual"),
            ("0", "Voltar"),
        ])
        if action in (None, "0"): return
        if action == "1":
            profiles = legacy_profiles(panel)
            active = panel.config_from_db().get("active_profile_id", "")
            lines = [f"Perfis XUI: {len(profiles)}", ""]
            for item in profiles:
                marker = "ATIVO" if item.get("profile_id") == active else "INATIVO"
                lines.append(f"[{marker}] {item.get('name', 'Perfil sem nome')}")
                lines.append("  " + panel.profile_status(item, active))
            message("\n".join(lines) if profiles else "Nenhum perfil XUI atual cadastrado.")
        elif action == "2":
            profile = choose_legacy_profile(panel, "Consultar perfil XUI atual")
            if profile:
                balancers = "\n".join(profile.get("load_balancers", [])) or "Nenhum"
                message(legacy_profile_status(panel, profile) + "\n\nBalanceadores de origem:\n" + balancers)
        elif action == "3": edit_legacy_profile(panel)
        elif action == "4":
            profile = choose_legacy_profile(panel, "Editar perfil XUI atual")
            if profile: edit_legacy_profile(panel, profile)
        elif action == "5": delete_legacy_profile(panel)
        elif action == "6":
            test = subprocess.run(["nginx", "-t"], capture_output=True, text=True, check=False)
            if test.returncode != 0:
                message("A validação do Nginx falhou. O recarregamento foi cancelado.\n\n" + test.stderr)
            else:
                reload_result = subprocess.run(["systemctl", "reload", "nginx"], check=False)
                message("Proxy validado e recarregado." if reload_result.returncode == 0
                        else "Não foi possível recarregar o proxy.")


def dashboard(db: Database) -> None:
    edges = db.edges(); tenants = db.tenants(); dns = db.dns_records()
    try:
        nodes = db.rows("SELECT id,name,ipv4,role,state FROM nodes ORDER BY CAST(id AS INTEGER),id")
    except Exception:
        nodes = []
    deployments = db.rows("SELECT id,state,release_id FROM deployments ORDER BY created_at DESC LIMIT 5")
    lines = [
        "PLANO DE CONTROLE",
        f"Edges: {len(edges)} (prontas: {sum(x['state'] == 'ready' for x in edges)})",
        f"Servidores registrados: {len(nodes)}",
        f"XUIs/tenants: {len(tenants)}",
        f"Registros DNS ativos: {sum(x['status'] == 'active' for x in dns)}",
        f"Painel web: {service_state('cdnmnus-admin.service')}",
        f"Orquestrador: {service_state('cdnmnus-orchestrator.service')}",
        f"Nginx: {service_state('nginx.service')}",
        "",
        "IMPLANTAÇÕES RECENTES",
    ]
    lines.extend(f"{x['id']}  {state_label(x['state'])}  {x['release_id']}" for x in deployments)
    if not deployments: lines.append("Nenhuma implantação registrada.")
    message("\n".join(lines))


def node_list(db: Database) -> None:
    rows = db.rows("SELECT id,name,ipv4,role,state FROM nodes ORDER BY CAST(id AS INTEGER),id")
    role_labels = {"control_plane": "Plano de controle", "edge": "Edge",
                   "load_balancer": "Load Balancer"}
    lines = [
        f"ID {item['id']} | {item['name']} | {item['ipv4']} | "
        f"{role_labels.get(item['role'], item['role'])} | {state_label(item['state'])}"
        for item in rows
    ]
    message("SERVIDORES REGISTRADOS\n\n" + ("\n".join(lines) if lines else "Nenhum servidor registrado."))


def edge_list(db: Database) -> None:
    edges = db.edges()
    lines = [f"{x['id']} | {x['name']} | {x['ipv4']}:{x['ssh_port']} | {state_label(x['state'])} | {x['deployed_version'] or '-'}" for x in edges]
    message("EDGES\n\n" + ("\n".join(lines) if lines else "Nenhuma edge cadastrada."))


def node_add(db: Database) -> None:
    role = choose("Papel inicial", [
        ("edge", "Edge — recebe runtime e deployment isolado"),
        ("load_balancer", "Load Balancer — entra somente como candidate"),
    ])
    if role is None: return
    name = ask("Nome da nova máquina")
    if name is None: return
    ipv4 = ask("IPv4 público da nova máquina")
    if ipv4 is None: return
    port_raw = ask("Porta SSH", "22")
    if port_raw is None: return
    initial_user = ask("Usuário SSH inicial", "root")
    if initial_user is None: return
    if not confirm(
        f"Cadastrar {name.strip()} ({ipv4}:{port_raw}) como {role}?\n\n"
        "O control plane capturará a host key duas vezes, fixará por TOFU auditado, "
        "instalará somente a tag aprovada e não ativará um LB automaticamente."
    ):
        return
    password = ask("Senha inicial (não será armazenada)", password=True)
    if password is None: return
    try:
        result = onboard_node(
            db, name=name, ipv4=ipv4, ssh_port=int(port_raw),
            initial_user=initial_user, password=password, role=role,
            operator="control-plane-menu", control_plane="143.14.168.111",
        )
    finally:
        password = ""; del password; gc.collect()
    deployment = result.get("deployment_id") or "não aplicável ao LB candidate"
    message(
        f"Máquina {result['node_id']} cadastrada como {result['role']}.\n"
        f"Estado: {result['state']}\nPacote: {result['package_ref']}\n"
        f"Deployment: {deployment}\n\n"
        "Senha descartada; host key fixada. LB não foi ativado."
    )


def edge_action(db: Database, state: str) -> None:
    edges = db.edges()
    selected = choose("Selecionar edge", [(x["id"], f"{x['name']} — {x['state']}") for x in edges])
    if selected is None: return
    if confirm(f"Alterar {selected} para {state}?"):
        db.set_edge_state(selected, state); db.sync_dns_matrix()
        message(f"Edge {selected}: {state}. Matriz DNS recalculada.")


def edge_rename(db: Database) -> None:
    edges = db.edges()
    selected = choose("Selecionar edge para renomear", [
        (x["id"], f"{x['name']} — {x['ipv4']} — {x['state']}") for x in edges
    ])
    if selected is None:
        return
    edge = db.edge(selected)
    new_name = ask(
        "Novo nome amigável\n\nO ID técnico não será alterado e continuará sendo " + selected,
        edge["name"],
    )
    if new_name is None or new_name.strip() == edge["name"]:
        return
    if not confirm(
        f"Renomear somente a exibição?\n\n"
        f"ID técnico preservado: {selected}\n"
        f"Nome atual: {edge['name']}\n"
        f"Novo nome: {new_name.strip()}\n\n"
        "IP, SSH, inventário, releases, DNS e serviços não serão alterados."
    ):
        return
    renamed = db.rename_edge(selected, new_name, operator="mago-cdn-menu",
                             reason="renomeação amigável solicitada pelo operador")
    message(
        f"Edge renomeada para {renamed['name']}.\n\n"
        f"ID técnico preservado: {renamed['id']}\n"
        "Nenhuma configuração de rede ou serviço foi alterada."
    )


def edges_menu(db: Database) -> None:
    while True:
        action = choose("Edges — distribuição e entrega", [
            ("1", "Consultar edges, estados e versões"),
            ("2", "Cadastrar nova máquina como Edge ou Load Balancer"),
            ("3", "Renomear edge (preserva o ID técnico)"),
            ("4", "Iniciar drenagem de uma edge"),
            ("5", "Marcar edge como pronta"),
            ("6", "Desabilitar uma edge"),
            ("0", "Voltar"),
        ])
        try:
            if action in (None, "0"): return
            if action == "1": edge_list(db)
            elif action == "2": node_add(db)
            elif action == "3": edge_rename(db)
            elif action == "4": edge_action(db, "draining")
            elif action == "5": edge_action(db, "ready")
            elif action == "6": edge_action(db, "disabled")
        except Exception as exc: message("Falha na operação de edge:\n\n" + str(exc))


def tenant_list(db: Database) -> None:
    tenants = db.tenants()
    lines = []
    for item in tenants:
        lines.append(f"{item['id']} | {item['name']} | {item['canonical_host']} | v{item['config_version']}")
        lines.extend(f"  - {host['hostname']} | TLS {host['tls_status']}" for host in item["hosts"])
        lines.extend(f"  - origem {up['kind']}: {up['host']}:{up['port']}" for up in item["upstreams"])
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
        action = choose("XUIs e domínios — nova arquitetura", [
            ("1", "Consultar XUIs, domínios e origens"),
            ("2", "Cadastrar novo XUI/tenant"),
            ("3", "Adicionar domínio alternativo (CNAME)"),
            ("4", "Visualizar configuração Nginx gerada"),
            ("0", "Voltar"),
        ])
        try:
            if action in (None, "0"): return
            if action == "1": tenant_list(db)
            elif action == "2": tenant_add(db)
            elif action == "3": tenant_cname(db)
            elif action == "4": tenant_vhost(db)
        except Exception as exc: message("Falha na operação de tenant:\n\n" + str(exc))


def vod_menu(db: Database) -> None:
    """Gerencia fontes VOD isoladas por tenant/XUI."""
    while True:
        action = choose("Fontes VOD por XUI", [
            ("list", "Listar fontes VOD cadastradas"),
            ("add", "Adicionar fonte VOD"),
            ("edit", "Editar fonte VOD"),
            ("delete", "Apagar fonte VOD"),
            ("back", "Voltar"),
        ])
        try:
            if action in (None, "back"): return
            tenants = db.tenants()
            items = [(t["id"], u) for t in tenants for u in t["upstreams"] if u["kind"] == "vod"]
            if action == "list":
                message("FONTES VOD\n\n" + ("\n".join(f"{tid} | {u['host']}:{u['port']} | {u['id']}" for tid, u in items) or "Nenhuma fonte VOD cadastrada."))
            elif action == "add":
                tenant = choose("Tenant/XUI", [(t["id"], t["canonical_host"]) for t in tenants])
                if tenant is None: continue
                host = ask("Domínio VOD autorizado"); port = ask("Porta", "80")
                if host is not None and port is not None:
                    db.add_upstream(tenant, "vod", host, int(port)); message("Fonte VOD adicionada.\n\nAcesse Implantações → Compilar versão para publicar.")
            elif action in ("edit", "delete"):
                selected = choose("Selecionar fonte VOD", [(u["id"], f"{tid} — {u['host']}:{u['port']}") for tid, u in items])
                if selected is None: continue
                current = next(u for _, u in items if u["id"] == selected)
                if action == "edit":
                    host = ask("Novo domínio VOD", current["host"]); port = ask("Nova porta", str(current["port"]))
                    if host is not None and port is not None:
                        db.update_upstream(selected, host, int(port)); message("Fonte VOD atualizada. Execute uma implantação para publicar.")
                elif confirm(f"Apagar {current['host']} deste tenant?"):
                    db.delete_upstream(selected); message("Fonte VOD removida. Execute uma implantação para publicar.")
        except Exception as exc: message("Falha na operação VOD:\n\n" + str(exc))


def dns_menu(db: Database) -> None:
    try:
        matrix = db.sync_dns_matrix()
        lines = [f"{x['hostname']} -> [{', '.join(x['targets']) or 'sem edge pronta'}] | TLS {x['tls_status']}" for x in matrix]
        message("MATRIZ DNS RECALCULADA\n\n" + ("\n".join(lines) if lines else "Nenhum hostname habilitado."))
    except Exception as exc: message("Falha no DNS:\n\n" + str(exc))


def deployments_menu(db: Database) -> None:
    while True:
        action = choose("Implantações e versões", [
            ("list", "Consultar implantações recentes"),
            ("queue", "Compilar versão e enfileirar implantação sequencial"),
            ("back", "Voltar"),
        ])
        try:
            if action in (None, "back"): return
            if action == "list":
                rows = db.rows("SELECT id,state,release_id,error,created_at FROM deployments ORDER BY created_at DESC LIMIT 30")
                message("\n".join(f"{x['created_at']} | {state_label(x['state'])} | {x['release_id']}\n{x['id']}\n{x['error'] or ''}" for x in rows) or "Nenhuma implantação.", 26)
            elif action == "queue" and confirm("Compilar a configuração atual e enfileirar a implantação sequencial?"):
                result = queue_deployment(db)
                message(f"Implantação adicionada à fila:\n{result['deployment_id']}\nVersão: {result['release_id']}")
        except Exception as exc: message("Falha no deployment:\n\n" + str(exc))


def promotion_requests_menu(db: Database) -> None:
    while True:
        requests = db.promotion_requests()
        action = choose("Solicitações edge → Load Balancer", [
            ("list", "Consultar solicitações e estados"),
            ("prepare", "Aprovar e preparar candidate/standby"),
            ("reject", "Rejeitar solicitação pendente"),
            ("back", "Voltar"),
        ])
        try:
            if action in (None, "back"):
                return
            if action == "list":
                lines = [
                    f"{item['id']} | nó {item['node_id']} | {item['requested_mode']} | "
                    f"{item['state']} | {item['package_ref']}"
                    for item in requests
                ]
                message("SOLICITAÇÕES DE PROMOÇÃO\n\n" + ("\n".join(lines) or "Nenhuma solicitação."), 26)
            elif action == "reject":
                pending = [item for item in requests if item["state"] == "requested"]
                selected = choose("Rejeitar solicitação", [
                    (item["id"], f"nó {item['node_id']} — {item['requested_mode']}")
                    for item in pending
                ])
                if selected and confirm("Rejeitar sem alterar a máquina?"):
                    db.set_promotion_request_state(selected, "rejected")
                    message("Solicitação rejeitada. Nenhum papel ou serviço foi alterado.")
            elif action == "prepare":
                pending = [item for item in requests if item["state"] == "requested"]
                selected = choose("Preparar candidato/standby", [
                    (item["id"], f"nó {item['node_id']} — {item['requested_mode']}")
                    for item in pending
                ])
                if selected is None:
                    continue
                config = ask(
                    "Arquivo root-only 0600 com TLS, backends e versão HAProxy",
                    f"/etc/cdnmnus/promotions/{selected}.json",
                )
                if config is None or not confirm(
                    "A edge será drenada e removida da matriz DNS.\n"
                    "O resultado será apenas candidate/standby; ACTIVE não será executado.\n\n"
                    "Continuar?"
                ):
                    continue
                result = subprocess.run(
                    [str(ROOT / "scripts/process_promotion_request.py"),
                     "--request-id", selected, "--config", config, "--confirm"],
                    capture_output=True, text=True, timeout=1800, check=False,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout).strip().splitlines()
                    raise RuntimeError(detail[-1] if detail else "preparação LB falhou")
                message("PREPARAÇÃO CONCLUÍDA\n\n" + result.stdout.strip())
        except Exception as exc:
            message("Falha na solicitação de promoção:\n\n" + str(exc))


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


def xui_content_menu(db: Database) -> None:
    while True:
        action = choose("XUIs, domínios e conteúdo", [
            ("1", "Nova arquitetura: XUIs, tenants e domínios"),
            ("2", "Fontes VOD isoladas por XUI"),
            ("3", "Configuração XUI atual (funções do menu anterior)"),
            ("0", "Voltar"),
        ])
        try:
            if action in (None, "0"): return
            if action == "1": tenants_menu(db)
            elif action == "2": vod_menu(db)
            elif action == "3": current_xui_menu()
        except Exception as exc:
            message("Falha na operação de XUI/conteúdo:\n\n" + str(exc))


def infrastructure_menu(db: Database) -> None:
    while True:
        action = choose("Infraestrutura e distribuição", [
            ("1", "Servidores registrados, IDs, papéis e estados"),
            ("2", "Máquinas — cadastro Edge/LB e operação das edges"),
            ("3", "DNS e matriz de distribuição"),
            ("4", "Implantações, versões e execução sequencial"),
            ("5", "Solicitações para preparar Load Balancer"),
            ("0", "Voltar"),
        ])
        if action in (None, "0"): return
        if action == "1": node_list(db)
        elif action == "2": edges_menu(db)
        elif action == "3": dns_menu(db)
        elif action == "4": deployments_menu(db)
        elif action == "5": promotion_requests_menu(db)


def operations_menu(db: Database) -> None:
    while True:
        action = choose("Operação e acesso", [
            ("1", "Serviços, diagnóstico e Nginx"),
            ("2", "Acesso ao painel web e porta local"),
            ("0", "Voltar"),
        ])
        if action in (None, "0"): return
        if action == "1": services_menu()
        elif action == "2": access_menu(db)


def main() -> int:
    if os.geteuid() != 0:
        print("Execute como root via SSH.", file=sys.stderr); return 1
    if shutil.which("whiptail") is None:
        print("whiptail não está instalado.", file=sys.stderr); return 1
    db = Database(DB_PATH); db.initialize()
    TopologyStore(db).initialize()
    while True:
        action = choose("Menu principal", [
            ("1", "Visão geral e situação do ambiente"),
            ("2", "Infraestrutura e distribuição"),
            ("3", "XUIs, domínios e conteúdo"),
            ("4", "Operação, serviços e acesso"),
            ("0", "Sair"),
        ], 20)
        try:
            if action in (None, "0"): return 0
            if action == "1": dashboard(db)
            elif action == "2": infrastructure_menu(db)
            elif action == "3": xui_content_menu(db)
            elif action == "4": operations_menu(db)
        except Exception as exc:
            message("Operação falhou:\n\n" + str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
