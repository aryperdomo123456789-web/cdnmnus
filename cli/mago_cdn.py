#!/usr/bin/env python3
"""Menu whiptail unificado: legado e plano de controle multi-edge."""
from __future__ import annotations

import gc
import importlib.util
import ipaddress
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
from core.cloudflare_dns import CloudflareDNS, CloudflareError
from core.control_plane import resolve_control_plane_host
from core.deploy import queue_deployment
from core.node_onboarding import onboard_node
from core.dns_reconciler import DNSReconciler, reconcile_cluster_dns
from core.render_tenants import render_tenant
from core.tenant_onboarding import TenantOnboardingService
from core.xui_discovery import discover_xui_media
from core.topology import TopologyStore

DB_PATH = os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db")
LEGACY_PANEL = "/opt/cdnmnus-panel/panel.py"
LOCAL_NODE_ID = Path("/etc/cdnmnus/node-id")
LOCAL_NODE_ROLE = Path("/etc/cdnmnus/node-role.json")
LOCAL_SSH_DIR = Path("/etc/cdnmnus/ssh")
LAB_PLAYER_DIR = ROOT / "lab-player"


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


def local_node_id() -> str | None:
    try:
        return LOCAL_NODE_ID.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def local_node_role() -> dict[str, object]:
    try:
        return json.loads(LOCAL_NODE_ROLE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _shorten(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _ssh_read_only_probe(node: dict[str, object], command: str, timeout: int = 60) -> tuple[int, str, str]:
    ssh_key = LOCAL_SSH_DIR / f"{node['id']}.ed25519"
    ssh_user = str(node.get("ssh_user") or "cdn-deploy")
    ssh_port = str(int(node.get("ssh_port") or 22))
    known_hosts = LOCAL_SSH_DIR / "known_hosts"
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", f"ConnectTimeout={timeout}",
        "-i", str(ssh_key),
        "-p", ssh_port,
        f"{ssh_user}@{node['ipv4']}",
        command,
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 10, check=False)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _run_manual_failover_preflight(node: dict[str, object]) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    probe_commands = [
        ("haproxy", "sudo -n haproxy -c -f /etc/haproxy/haproxy.cfg"),
        ("nginx", "sudo -n nginx -t"),
        ("services", "sudo -n systemctl is-active nginx cdnmnus-admin cdnmnus-orchestrator"),
    ]
    for label, command in probe_commands:
        code, stdout, stderr = _ssh_read_only_probe(node, command)
        ok = code == 0
        if label == "services":
            ok = ok and all(line.strip() == "active" for line in stdout.splitlines() if line.strip())
        checks.append({
            "check": label,
            "ok": ok,
            "command": command,
            "stdout": _shorten(stdout, 500),
            "stderr": _shorten(stderr, 500),
        })
        if not ok:
            raise RuntimeError(f"preflight read-only falhou em {label}: {stderr or stdout or 'sem saída'}")
    return {"checks": checks, "ok": True}


def _run_manual_failover_lab() -> dict[str, object]:
    xuilab_env = Path("/etc/cdnmnus/lab-player/xuilab.env")
    run_xuilab = LAB_PLAYER_DIR / "scripts" / "run_xuilab_test.sh"
    test_playback = LAB_PLAYER_DIR / "scripts" / "test_playback_flow.py"
    if run_xuilab.is_file() and xuilab_env.is_file():
        command = [str(run_xuilab)]
    else:
        env = {
            "PLAYER_USERNAME": os.environ.get("PLAYER_USERNAME", ""),
            "PLAYER_PASSWORD": os.environ.get("PLAYER_PASSWORD", ""),
            "PLAYER_BASE_CDN": os.environ.get("PLAYER_BASE_CDN", ""),
            "PLAYER_BASE_DIRECT": os.environ.get("PLAYER_BASE_DIRECT", ""),
            "PLAYER_BASE_CNAME": os.environ.get("PLAYER_BASE_CNAME", ""),
        }
        if not env["PLAYER_USERNAME"] or not env["PLAYER_PASSWORD"]:
            raise RuntimeError("laboratório indisponível: credenciais do player ausentes")
        if env["PLAYER_BASE_CNAME"] and env["PLAYER_BASE_CDN"] and env["PLAYER_BASE_DIRECT"]:
            command = [
                sys.executable, str(test_playback),
                "--cname",
                "--refresh-samples",
            ]
        elif env["PLAYER_BASE_CDN"] and env["PLAYER_BASE_DIRECT"]:
            command = [
                sys.executable, str(test_playback),
                "--both",
                "--refresh-samples",
            ]
        else:
            raise RuntimeError("laboratório indisponível: bases CDN/DIRECT/CNAME incompletas")
    result = subprocess.run(command, capture_output=True, text=True, timeout=5400, check=False, cwd=ROOT)
    if result.returncode != 0:
        detail = _shorten("\n".join((result.stderr or result.stdout).splitlines()[-40:]), 2000)
        raise RuntimeError(f"laboratório falhou: {detail}")
    return {
        "command": command,
        "stdout": _shorten(result.stdout, 1000),
    }


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


def capacity_snapshot(db: Database) -> list[dict]:
    topology = TopologyStore(db)
    topology.initialize()
    return topology.capacity_snapshot()


def capacity_overview(db: Database) -> None:
    snapshot = capacity_snapshot(db)
    lines = [
        "CAPACIDADE E CONSUMO",
        f"Nós: {len(snapshot)}",
        f"Capacidade contratada: {sum(item['capacity_mbps'] or 0 for item in snapshot)} Mbps",
        f"Capacidade útil: {round(sum(item['usable_mbps'] or 0 for item in snapshot), 2)} Mbps",
        f"Tráfego medido: {round(sum(item['tx_mbps'] or 0 for item in snapshot), 2)} Mbps",
        f"Pressão máxima: {round(max((item['pressure'] or 0 for item in snapshot), default=0), 4)}",
        "",
    ]
    for item in snapshot:
        node = item["node"]
        runtime = item.get("runtime") or {}
        lines.append(
            f"{node['id']:>4} | {node['name']:<24.24} | {node['role']:<14} | {node['state']:<11} | "
            f"{item.get('capacity_mbps') or '-':>5} Mbps | {item.get('tx_mbps') or 0:>7.2f} Mbps | "
            f"peso {runtime.get('applied_weight', '-')}"
        )
    message("\n".join(lines), 28)


def capacity_choose_node(db: Database) -> dict | None:
    snapshot = capacity_snapshot(db)
    if not snapshot:
        message("Nenhum nó cadastrado.")
        return None
    selected = choose("Selecionar nó", [
        (item["node"]["id"], f"{item['node']['name']} | {item['node']['ipv4']} | {item['node']['role']} | {item['node']['state']}")
        for item in snapshot
    ], 22)
    if selected is None:
        return None
    return next((item for item in snapshot if item["node"]["id"] == selected), None)


def capacity_detail(db: Database) -> None:
    item = capacity_choose_node(db)
    if item is None:
        return
    node = item["node"]
    profile = item.get("profile") or {}
    sample = item.get("sample") or {}
    runtime = item.get("runtime") or {}
    lines = [
        f"{node['id']} | {node['name']} | {node['ipv4']}",
        f"Papel: {node['role']} | Estado: {node['state']}",
        f"Capacidade contratada: {item.get('capacity_mbps') or 'n/a'} Mbps",
        f"Capacidade útil: {item.get('usable_mbps') or 'n/a'} Mbps",
        f"Consumo atual: {item.get('tx_mbps') or 'n/a'} Mbps",
        f"Pressão: {item.get('pressure') or 'n/a'} | Runtime: {runtime.get('state', 'n/a')} | Peso: {runtime.get('applied_weight', 'n/a')}",
        f"Perfil: {profile.get('confidence', 'n/a')} | Fonte: {profile.get('source', 'n/a')}",
        f"Amostra: {sample.get('sampled_at', 'n/a')} | Idade: {item.get('sample_age_seconds', 'n/a')}s",
    ]
    if sample:
        lines.extend([
            f"CPU: {sample.get('cpu_pct', 'n/a')}%",
            f"Memória: {sample.get('mem_pct', 'n/a')}%",
            f"Sessões ativas: {sample.get('active_sessions', 'n/a')}",
            f"p95: {sample.get('p95_ms', 'n/a')} ms",
            f"HTTP 5xx: {sample.get('http5xx', 'n/a')}",
            f"NIC errors: {sample.get('nic_errors', 'n/a')}",
        ])
    message("\n".join(lines), 24)


def capacity_profile_update(db: Database) -> None:
    item = capacity_choose_node(db)
    if item is None:
        return
    node = item["node"]
    raw = ask("Capacidade contratada em Mbps", str(item.get("capacity_mbps") or 1000))
    if raw is None or not raw.strip():
        return
    headroom = ask("Headroom", str((item.get("profile") or {}).get("headroom", 0.25)))
    if headroom is None or not headroom.strip():
        return
    max_connections = ask("Conexões máximas", str((item.get("profile") or {}).get("max_connections", 0)))
    if max_connections is None or not max_connections.strip():
        return
    source = ask("Fonte", (item.get("profile") or {}).get("source", "manual")) or "manual"
    confidence = ask("Confiança", (item.get("profile") or {}).get("confidence", "manual")) or "manual"
    measured = ask("Capacidade medida opcional", str((item.get("profile") or {}).get("measured_mbps", "")) or "")
    measured_at = ask("Timestamp medido opcional", (item.get("profile") or {}).get("measured_at", "")) or ""
    expires_at = ask("Validade opcional", (item.get("profile") or {}).get("expires_at", "")) or ""
    topology = TopologyStore(db); topology.initialize()
    result = topology.set_capacity_profile(
        node["id"], int(raw), source=source, confidence=confidence, headroom=float(headroom),
        max_connections=int(max_connections),
        measured_mbps=int(measured) if measured else None,
        measured_at=measured_at or None,
        expires_at=expires_at or None,
    )
    message(
        "Perfil de capacidade atualizado.\n\n"
        f"Nó: {result['node_id']}\n"
        f"Capacidade: {result['capacity_mbps']} Mbps\n"
        f"Headroom: {result['headroom']}\n"
        f"Fonte: {result['source']}\n"
        f"Confiança: {result['confidence']}"
    )


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
            operator="control-plane-menu", control_plane=resolve_control_plane_host(require_explicit=True),
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


def node_add_edge_minimal(db: Database) -> None:
    """Cadastra uma edge nova com somente os dados indispensáveis de SSH."""
    ipv4 = ask("IPv4 público da nova edge")
    if ipv4 is None:
        return
    try:
        address = ipaddress.ip_address(ipv4.strip())
        if address.version != 4 or not address.is_global:
            raise ValueError
    except ValueError:
        message("Informe um IPv4 público global. Hostname, IPv6 e IP privado não são aceitos.")
        return
    name = "edge-" + str(address).replace(".", "-")
    port_raw = ask("Porta SSH", "22")
    if port_raw is None:
        return
    initial_user = ask("Usuário SSH inicial", "root")
    if initial_user is None:
        return
    if not confirm(
        f"Cadastrar {name} ({address}:{port_raw}) como Edge?\n\n"
        "O nome e o papel serão definidos automaticamente. O control plane "
        "capturará a host key, criará a chave gerenciada e instalará somente "
        "a release aprovada. A edge só entrará no DNS depois de ficar ready."
    ):
        return
    password = ask("Senha inicial (não será armazenada)", password=True)
    if password is None:
        return
    try:
        result = onboard_node(
            db, name=name, ipv4=str(address), ssh_port=int(port_raw),
            initial_user=initial_user, password=password, role="edge",
            operator="control-plane-menu",
            control_plane=resolve_control_plane_host(require_explicit=True),
        )
    finally:
        password = ""
        del password
        gc.collect()
    deployment = result.get("deployment_id") or "aguardando tenants habilitados"
    message(
        f"Edge {result['node_id']} cadastrada.\n"
        f"Nome: {name}\nEstado: {result['state']}\n"
        f"Pacote: {result['package_ref']}\nDeployment: {deployment}\n\n"
        "Senha descartada; host key e chave gerenciada foram fixadas. "
        "A promoção para ready depende dos gates automáticos."
    )


def edge_action(db: Database, state: str) -> None:
    edges = db.edges()
    selected = choose("Selecionar edge", [(x["id"], f"{x['name']} — {x['state']}") for x in edges])
    if selected is None: return
    if confirm(f"Alterar {selected} para {state}?"):
        db.set_edge_state(selected, state); db.sync_dns_matrix()
        try:
            provider = CloudflareDNS()
            edges = [x for x in db.edges() if x["state"] == "ready"]
            records = DNSReconciler(provider, db=db, operator="mago-cdn-menu").repair_canonical_pool(
                "cdn.phpd77.com", [x["ipv4"] for x in edges],
                forbidden_ips={"143.14.168.111"},
            )
            message(f"Edge {selected}: {state}. Cloudflare reconciliado.\n\n" +
                    "\n".join(f"{x['name']} A {x['content']} DNS-only" for x in records))
        except CloudflareError as exc:
            message(f"Edge {selected}: {state}. Matriz local recalculada, mas Cloudflare não foi aplicado.\n\n{exc}")


def manual_controller_failover(db: Database) -> None:
    topology = TopologyStore(db)
    topology.initialize()
    current_node = local_node_id()
    if current_node != "1":
        message(
            "A operação está travada para o nó 1 neste ambiente.\n\n"
            f"Nó local detectado: {current_node or 'indisponível'}\n"
            "Use apenas o control plane autorizado para este failover."
        )
        return
    source = topology.node("1")
    target = topology.node("4")
    if source["role"] != "load_balancer" or target["role"] != "load_balancer":
        message(
            "Topologia inconsistente para o failover manual.\n\n"
            f"Origem: {source['id']} ({source['role']}/{source['state']})\n"
            f"Destino: {target['id']} ({target['role']}/{target['state']})"
        )
        return
    source_lb = db.rows("SELECT * FROM load_balancers WHERE node_id=?", (source["id"],))
    target_lb = db.rows("SELECT * FROM load_balancers WHERE node_id=?", (target["id"],))
    if (source["state"] != "active" or not source_lb or source_lb[0]["state"] != "active"):
        message(
            "A origem não está active no estado autoritativo. O failover foi bloqueado.\n\n"
            f"Origem: node {source['id']} ({source['state']}/{source_lb[0]['state'] if source_lb else 'sem LB'})\n"
            "Confirme o estado pelo control plane antes de qualquer operação."
        )
        return
    if target["state"] != "standby":
        message(
            "O destino não está em standby e a operação foi recusada.\n\n"
            f"Destino atual: {target['id']} ({target['state']})"
        )
        return
    if not target_lb or target_lb[0]["state"] != "standby":
        message(
            "A configuração LB do destino não está em standby. O failover foi bloqueado.\n\n"
            f"Destino: node {target['id']} ({target['state']}/{target_lb[0]['state'] if target_lb else 'sem LB'})"
        )
        return
    active_lbs = db.rows("SELECT * FROM load_balancers WHERE state='active'")
    if len(active_lbs) != 1 or active_lbs[0]["node_id"] != source["id"]:
        message(
            "A topologia não tem exatamente a origem como único LB active. O failover manual foi bloqueado."
        )
        return
    motivo = ask("Motivo obrigatório")
    if motivo is None or not motivo.strip():
        return
    isolation_reference = ask("Evidência de isolamento")
    if isolation_reference is None or not isolation_reference.strip():
        return
    confirmation = ask("Confirmação", "")
    if confirmation is None or confirmation.strip() != "CONFIRMO_ISOLAMENTO_DO_111":
        message("Confirmação incorreta. A operação foi cancelada.")
        return
    summary = "\n".join([
        "FAILOVER MANUAL DO CONTROLADOR DNS",
        "",
        f"Origem: {source['id']} / {source['ipv4']}",
        f"Destino: {target['id']} / {target['ipv4']}",
        "Função: controlador DNS; sem tráfego de mídia",
        "Edges esperadas: .168, .170, .78",
        "",
        f"Motivo: {motivo.strip()}",
        f"Evidência de isolamento: {isolation_reference.strip()}",
        "",
        "A operação vai executar preflight read-only, adquirir lease, promover o destino, reconciliar DNS e validar o laboratório.",
    ])
    message(summary, 22)
    if not confirm("Confirmar o failover manual agora?\n\nAção irreversível nesta sessão."):
        return
    event_payload: dict[str, object] = {
        "operation": "manual_dns_controller_failover",
        "source_node": source["id"],
        "target_node": target["id"],
        "reason": motivo.strip(),
        "isolation_reference": isolation_reference.strip(),
        "from_state": target["state"],
        "to_state": "active",
    }
    try:
        preflight = _run_manual_failover_preflight(target)
        event_payload["preflight"] = preflight
        lease = topology.acquire_promotion_lock(
            "manual_dns_controller_failover",
            target["id"],
            "mago-cdn-menu",
            motivo.strip(),
            30,
        )
        event_payload["lease_id"] = lease["lease_id"]
        event_payload["fencing_token"] = lease["fencing_token"]
        topology.demote_load_balancer(
            source["id"],
            "standby",
            "mago-cdn-menu",
            motivo.strip(),
        )
        event_payload["source_demoted"] = True
        promoted = topology.promote_load_balancer(
            target["id"],
            "manual_dns_controller_failover",
            lease["lease_id"],
            lease["fencing_token"],
            "mago-cdn-menu",
            motivo.strip(),
        )
        event_payload["promotion_state"] = promoted["state"] if isinstance(promoted, dict) else "active"
        dns_result = reconcile_cluster_dns(db, operator="mago-cdn-menu", canonical=db.setting("managed_canonical_host", "cdn.phpd77.com"))
        event_payload["dns_result"] = dns_result
        lab_result = _run_manual_failover_lab()
        event_payload["lab_result"] = {"status": "ok", **lab_result}
        topology.record_manual_failover(target["id"], "mago-cdn-menu", motivo.strip(), event_payload)
        message(
            "Failover manual concluído com sucesso.\n\n"
            f"Destino ativo: {target['id']} / {target['ipv4']}\n"
            f"Lease: {lease['lease_id']}\n"
            f"Fencing token: {lease['fencing_token']}\n"
            "DNS e laboratório foram validados."
        )
    except Exception as exc:
        event_payload["error"] = _shorten(str(exc), 1000)
        event_payload["lab_result"] = {"status": "failed"} if "lab_result" not in event_payload else event_payload["lab_result"]
        try:
            topology.record_manual_failover(target["id"], "mago-cdn-menu", motivo.strip(), event_payload)
        except Exception:
            pass
        message(f"Failover manual interrompido:\n\n{_shorten(str(exc), 1200)}")


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
            ("2", "Adicionar nova Edge (cadastro mínimo: IP/SSH)"),
            ("3", "Cadastrar Edge ou Load Balancer (avançado)"),
            ("4", "Renomear edge (preserva o ID técnico)"),
            ("5", "Iniciar drenagem de uma edge"),
            ("6", "Marcar edge como pronta"),
            ("7", "Desabilitar uma edge"),
            ("0", "Voltar"),
        ])
        try:
            if action in (None, "0"): return
            if action == "1": edge_list(db)
            elif action == "2": node_add_edge_minimal(db)
            elif action == "3": node_add(db)
            elif action == "4": edge_rename(db)
            elif action == "5": edge_action(db, "draining")
            elif action == "6": edge_action(db, "ready")
            elif action == "7": edge_action(db, "disabled")
        except Exception as exc: message("Falha na operação de edge:\n\n" + str(exc))


def tenant_list(db: Database) -> None:
    tenants = db.tenants()
    lines = []
    for item in tenants:
        onboarding = db.tenant_onboarding(item['id'])
        onboarding_state = onboarding['state'] if onboarding else ("committed" if item['enabled'] else "disabled")
        lines.append(f"{item['id']} | {item['name']} | {item['canonical_host']} | v{item['config_version']} | onboarding {onboarding_state}")
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
    m3u_url = ask("URL M3U autorizada (não será armazenada)", password=True)
    if m3u_url is None: return
    extra_lbs = ask("Load balancers adicionais (opcional)", "")
    if extra_lbs is None: return
    try:
        discovery = discover_xui_media(m3u_url)
        lbs = tuple(dict.fromkeys(discovery.load_balancers + tuple(
            x.strip() for x in extra_lbs.split(",") if x.strip())))
        onboarding = TenantOnboardingService(db).register(
            tenant_id, name, canonical, origin, int(port),
            lbs, discovery.vod_seeds,
        )
        message(
            f"Tenant {tenant_id} cadastrado em onboarding seguro.\n\n"
            f"Estado: {onboarding['state']}\n"
            f"Descoberta M3U: {discovery.sampled_live} live + {discovery.sampled_vod} VOD; "
            f"{len(discovery.load_balancers)} LB e {len(discovery.vod_seeds)} seed VOD.\n"
            "Nenhum DNS, TLS ou runtime foi publicado. O tenant só será habilitado "
            "após os gates da transação operacional."
        )
    except (CloudflareError, ValueError) as exc:
        message(f"Cadastro recusado; nenhum estado público foi alterado.\n\n{exc}")


def tenant_cname(db: Database) -> None:
    tenants = db.tenants()
    selected = choose("Tenant do novo CNAME", [(x["id"], x["canonical_host"]) for x in tenants])
    if selected is None: return
    hostname = ask("Hostname/alias do cliente")
    if hostname is None: return
    tenant = db.add_cname(selected, hostname)
    tls_job = db.enqueue_tls_job(selected)
    try:
        records = DNSReconciler(CloudflareDNS(), db=db, operator="mago-cdn-menu").apply_tenant(db.tenant(selected))
        message(f"Alias {hostname} associado, TLS enfileirado e Cloudflare reconciliado.\n"
                f"Job TLS: {tls_job['id']}\n\n" +
                "\n".join(f"{x['name']} CNAME {x['content']} DNS-only" for x in records))
    except CloudflareError as exc:
        message(f"Alias {hostname} salvo localmente e TLS enfileirado, mas Cloudflare não foi aplicado.\n\n{exc}")


def tenant_vhost(db: Database) -> None:
    tenants = db.tenants()
    selected = choose("Visualizar vhost", [(x["id"], x["canonical_host"]) for x in tenants])
    if selected is None: return
    message(render_tenant(db.tenant(selected)).content, 28)


def tenant_resync_m3u(db: Database) -> None:
    tenants = db.tenants()
    selected = choose("XUI para ressincronizar M3U", [(x["id"], x["canonical_host"]) for x in tenants])
    if selected is None: return
    m3u_url = ask("URL M3U autorizada (não será armazenada)", password=True)
    if m3u_url is None: return
    try:
        discovery = discover_xui_media(m3u_url)
        if not confirm(
            f"Amostras aprovadas: {discovery.sampled_live} live + {discovery.sampled_vod} VOD.\n"
            f"Novo mapa: {len(discovery.load_balancers)} LB + {len(discovery.vod_seeds)} seed VOD.\n\n"
            "O tenant será fechado durante a troca e só voltará após TLS, deploy, health e DNS.\n\n"
            "Aplicar a ressincronização?"
        ):
            return
        onboarding = TenantOnboardingService(db).resync(
            selected, discovery.load_balancers, discovery.vod_seeds,
        )
        message(
            f"M3U do {selected} ressincronizada com segurança.\n\n"
            f"Estado: {onboarding['state']}\n"
            f"Mapa aplicado: {len(discovery.load_balancers)} LB + {len(discovery.vod_seeds)} seed VOD.\n"
            "O worker executará automaticamente os gates antes de republicar o tenant."
        )
    except (CloudflareError, ValueError) as exc:
        message(f"Ressincronização recusada; o mapa anterior permaneceu intacto.\n\n{exc}")


def tenants_menu(db: Database) -> None:
    while True:
        action = choose("XUIs e domínios — nova arquitetura", [
            ("1", "Consultar XUIs, domínios e origens"),
            ("2", "Cadastrar novo XUI/tenant"),
            ("3", "Adicionar domínio alternativo (CNAME)"),
            ("4", "Visualizar configuração Nginx gerada"),
            ("5", "Ressincronizar M3U autorizada"),
            ("0", "Voltar"),
        ])
        try:
            if action in (None, "0"): return
            if action == "1": tenant_list(db)
            elif action == "2": tenant_add(db)
            elif action == "3": tenant_cname(db)
            elif action == "4": tenant_vhost(db)
            elif action == "5": tenant_resync_m3u(db)
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


def cloudflare_reconcile(db: Database) -> None:
    result = reconcile_cluster_dns(db, operator="mago-cdn-menu")
    pool, aliases = result["pool"], result["aliases"]
    message("CLOUDFLARE RECONCILIADO\n\n" +
            "\n".join(f"{x['name']} {x['type']} {x['content']} DNS-only" for x in pool + aliases))


def cloudflare_configure() -> None:
    zones = ask(
        "Zonas Cloudflare autorizadas (separe por vírgula)",
        "phpd77.com",
    )
    if zones is None or not zones.strip():
        return
    token = ask("Token Cloudflare (não será exibido nem armazenado em banco)", password=True)
    if token is None or not token.strip():
        return
    try:
        provider = CloudflareDNS(token=token.strip(), zone=zones.strip())
        provider.verify()
        directory = Path("/etc/cdnmnus/cloudflare")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        token_path = directory / "api-token"
        zones_path = directory / "zones"
        token_path.write_text(token.strip() + "\n", encoding="utf-8")
        token_path.chmod(0o600)
        zones_path.write_text(zones.strip() + "\n", encoding="utf-8")
        zones_path.chmod(0o644)
        acme_directory = Path("/etc/cdnmnus/secrets")
        acme_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        acme_path = acme_directory / "cloudflare_acme.ini"
        acme_path.write_text(f"dns_cloudflare_api_token = {token.strip()}\n", encoding="utf-8")
        acme_path.chmod(0o600)
        token = ""
        message("Cloudflare configurada e token validado.\n\n" +
                "Zonas autorizadas: " + ", ".join(provider.zones) +
                "\nModo: DNS-only\n\nExecute agora a reconciliação para corrigir o pool canônico.")
    except CloudflareError as exc:
        message("Cloudflare não foi configurada.\n\n" + str(exc))
    finally:
        token = ""
        gc.collect()


def cloudflare_domain_switch(db: Database) -> None:
    """Migra a zona/domínio de forma aditiva, sem remover o domínio atual."""
    domain = ask("Novo domínio raiz (ex.: dominionovo.com)")
    if domain is None or not domain.strip():
        return
    domain = domain.strip().lower().rstrip(".")
    zones = ask("Zonas Cloudflare autorizadas", domain)
    if zones is None or not zones.strip():
        return
    token = ask("Novo token Cloudflare (não será exibido nem vai ao banco)", password=True)
    if token is None or not token.strip():
        return
    try:
        provider = CloudflareDNS(token=token.strip(), zone=zones.strip())
        provider.verify()
        provider.zone_for_name(f"cdn.{domain}")
        tenants = db.tenants(enabled_only=True)
        preview = [f"cdn.{domain}"] + [
            f"{item['canonical_host'].split('.', 1)[0]}.{domain} ({item['id']})"
            for item in tenants
        ]
        if not confirm(
            "Migrar a identidade pública para a nova zona?\n\n" +
            "Hosts derivados que serão preparados:\n" + "\n".join(preview) +
            "\n\nO domínio antigo será preservado como alias.\n"
            "Nenhum DNS antigo será apagado nesta operação."
        ):
            return
        directory = Path("/etc/cdnmnus/cloudflare")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / "api-token").write_text(token.strip() + "\n", encoding="utf-8")
        (directory / "api-token").chmod(0o600)
        (directory / "zones").write_text(zones.strip() + "\n", encoding="utf-8")
        (directory / "zones").chmod(0o644)
        acme_directory = Path("/etc/cdnmnus/secrets")
        acme_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        acme_path = acme_directory / "cloudflare_acme.ini"
        acme_path.write_text(f"dns_cloudflare_api_token = {token.strip()}\n", encoding="utf-8")
        acme_path.chmod(0o600)
        result = db.switch_managed_domain(domain)
        for item in tenants:
            db.enqueue_tls_job(item["id"])
        dns = reconcile_cluster_dns(db, operator="mago-cdn-domain-switch", canonical=result["canonical"])
        message(
            "MIGRAÇÃO PREPARADA COM SUCESSO\n\n" +
            f"Cloudflare: {', '.join(provider.zones)}\n" +
            f"Pool novo: {result['canonical']}\n" +
            "\n".join(f"{x['tenant_id']}: {x['new']}" for x in result["mappings"]) +
            f"\n\nDNS aplicados: {len(dns['pool']) + len(dns['aliases'])}\n"
            "TLS: jobs reenfileirados por tenant.\n"
            "Próximo passo obrigatório: executar ACME, compilar release, validar e promover as edges."
        )
    except (CloudflareError, ValueError) as exc:
        message("Migração cancelada; nenhuma release foi publicada.\n\n" + str(exc))
    finally:
        token = ""
        gc.collect()


def dns_menu(db: Database) -> None:
    while True:
        action = choose("DNS e Cloudflare", [
            ("1", "Ver matriz DNS local"),
            ("2", "Reconciliar Cloudflare agora"),
            ("3", "Configurar conta/token Cloudflare"),
            ("4", "Trocar Cloudflare e domínio com migração segura"),
            ("0", "Voltar"),
        ])
        if action in (None, "0"): return
        try:
            if action == "1":
                matrix = db.sync_dns_matrix()
                lines = [f"{x['hostname']} -> [{', '.join(x['targets']) or 'sem edge pronta'}] | TLS {x['tls_status']}" for x in matrix]
                message("MATRIZ DNS LOCAL\n\n" + ("\n".join(lines) if lines else "Nenhum hostname habilitado."))
            elif action == "2":
                if confirm("Aplicar o estado DNS desejado na Cloudflare?\n\nIsso removerá o control-plane do pool canônico e manterá DNS-only."):
                    cloudflare_reconcile(db)
            elif action == "3":
                cloudflare_configure()
            elif action == "4":
                cloudflare_domain_switch(db)
        except (CloudflareError, ValueError) as exc:
            message("Falha na reconciliação Cloudflare:\n\n" + str(exc))


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
            ("6", "Capacidade, consumo e pressão do cluster"),
            ("7", "Ajustar perfil de capacidade de um nó"),
            ("8", "Failover manual do controlador DNS"),
            ("0", "Voltar"),
        ])
        if action in (None, "0"): return
        if action == "1": node_list(db)
        elif action == "2": edges_menu(db)
        elif action == "3": dns_menu(db)
        elif action == "4": deployments_menu(db)
        elif action == "5": promotion_requests_menu(db)
        elif action == "6": capacity_overview(db)
        elif action == "7": capacity_profile_update(db)
        elif action == "8": manual_controller_failover(db)


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
