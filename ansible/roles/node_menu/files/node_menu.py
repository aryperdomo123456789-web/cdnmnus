#!/usr/bin/env python3
"""Cliente local comum; diagnóstico sem criar uma fonte de verdade paralela."""
import json
import os
import shlex
import ssl
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import urllib.request

ROLE_LABEL = {"control_plane": "PLANO DE CONTROLE", "edge": "EDGE", "load_balancer": "LOAD BALANCER"}
STATE_LABEL = {"bootstrapping": "EM PREPARAÇÃO", "ready": "PRONTA", "candidate": "CANDIDATO",
               "active": "ATIVO", "standby": "EM ESPERA", "draining": "EM DRENAGEM"}


def dialog(arguments):
    read_fd, write_fd = os.pipe()
    command = ["whiptail", "--title", "Mago CDN — Central de Operações", "--output-fd", str(write_fd), *arguments]
    result = subprocess.run(command, pass_fds=(write_fd,), check=False)
    os.close(write_fd)
    value = os.read(read_fd, 1024 * 1024).decode(errors="replace").strip()
    os.close(read_fd)
    return result.returncode, value


def message(text):
    dialog(["--scrolltext", "--msgbox", text, "20", "92"])


def ask(prompt, default="", password=False):
    kind = "--passwordbox" if password else "--inputbox"
    arguments = [kind, prompt, "10", "88"]
    if default and not password:
        arguments.append(default)
    code, value = dialog(arguments)
    return value if code == 0 else None


def service(name):
    result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, check=False)
    return result.stdout.strip() or "desconhecido"


def identity():
    try:
        data = json.loads(Path("/etc/cdnmnus/node-role.json").read_text())
        node_id = Path("/etc/cdnmnus/node-id").read_text().strip()
    except Exception as exc:
        raise RuntimeError("identidade local ausente ou inválida") from exc
    required = {"schema", "node_id", "name", "role", "state", "control_plane", "release_id", "config_digest"}
    if not required.issubset(data):
        raise RuntimeError("contrato local do nó incompleto")
    if str(data.get("node_id")) != node_id:
        raise RuntimeError("ID local diverge do arquivo de função")
    control_plane = data["control_plane"]
    if not isinstance(control_plane, dict):
        raise RuntimeError("contrato do plano de controle inválido")
    for key in ("host", "port", "scheme", "verify"):
        if key not in control_plane:
            raise RuntimeError("contrato do plano de controle incompleto")
    return data


def control_plane_host():
    cfg = {}
    for line in Path("/etc/cdnmnus/control-plane.conf").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip()
    for key in ("CONTROL_PLANE_HOST", "CONTROL_PLANE_PORT", "CONTROL_PLANE_SCHEME", "CONTROL_PLANE_VERIFY", "NODE_BOOTSTRAP_MODE"):
        if key not in cfg:
            raise RuntimeError("contrato local do plano de controle incompleto")
    return cfg["CONTROL_PLANE_HOST"]


def connectivity(host):
    try:
        with socket.create_connection((host, 22), timeout=2):
            return "alcançável"
    except OSError:
        return "indisponível"


def package_identity():
    path = Path("/var/lib/cdnmnus-node/package.json")
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        raise RuntimeError("pacote universal versionado não instalado neste nó") from exc
    required = {"ref", "commit", "manifest_digest"}
    if not required.issubset(data):
        raise RuntimeError("identidade do pacote universal incompleta")
    return data


def control_plane_ssh(host, remote_command, input_text=None, timeout=30):
    command = [
        "runuser", "-u", "cdn-deploy", "--", "ssh", "-T",
        "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
        "-o", "StrictHostKeyChecking=yes", f"cdn-deploy@{host}", remote_command,
    ]
    return subprocess.run(
        command, input=input_text, capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def control_plane_json(host, remote_command, input_text=None, timeout=30):
    result = control_plane_ssh(host, remote_command, input_text=input_text, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "control plane recusou a solicitação")
    return json.loads(result.stdout or "{}")


def open_control_plane_menu(host):
    """Abre o menu autoritativo no control-plane; o segredo nunca passa por este nó."""
    command = [
        "ssh", "-tt", "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
        "-o", "StrictHostKeyChecking=yes", f"cdn-deploy@{host}",
        "sudo -n /usr/local/bin/mago-cdn",
    ]
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise RuntimeError("não foi possível abrir o menu do control-plane")


def cluster_snapshot(host):
    return control_plane_json(host, "sudo -n /usr/local/sbin/cdnmnus-cluster-status", timeout=60)


def _node_title(node):
    return f"{node['id']:>4} | {node.get('name', ''):<24.24} | {node.get('ipv4', ''):<15} | {node.get('role', ''):<14} | {node.get('state', '')}"


def _format_capacity(item):
    node = item["node"]
    profile = item.get("profile") or {}
    sample = item.get("sample") or {}
    runtime = item.get("runtime") or {}
    capacity = item.get("capacity_mbps")
    usable = item.get("usable_mbps")
    tx = item.get("tx_mbps")
    pressure = item.get("pressure")
    consumption = item.get("consumption_pct")
    parts = [
        _node_title(node),
        f"  Capacidade: {capacity if capacity is not None else 'n/a'} Mbps | Útil: {usable if usable is not None else 'n/a'} Mbps",
        f"  Consumo: {tx if tx is not None else 'n/a'} Mbps | Uso da capacidade: {consumption if consumption is not None else 'n/a'}%",
        f"  Pressão: {pressure if pressure is not None else 'n/a'} | Runtime: {runtime.get('state', 'n/a')} | Peso: {runtime.get('applied_weight', 'n/a')}",
        f"  Perfil: {profile.get('confidence', 'n/a')} / {profile.get('source', 'n/a')} | Amostra: {sample.get('sampled_at', 'n/a')} | Idade: {item.get('sample_age_seconds', 'n/a')}s",
    ]
    if sample:
        parts.append(
            f"  CPU {sample.get('cpu_pct', 'n/a')}% | Mem {sample.get('mem_pct', 'n/a')}% | "
            f"Sessões {sample.get('active_sessions', 'n/a')} | p95 {sample.get('p95_ms', 'n/a')} ms | "
            f"5xx {sample.get('http5xx', 'n/a')} | NIC errors {sample.get('nic_errors', 'n/a')}"
        )
    return "\n".join(parts)


def cluster_overview(host):
    snapshot = cluster_snapshot(host)
    lines = [
        "VISÃO CONSOLIDADA DO CLUSTER",
        f"Gerado em: {snapshot.get('generated_at', 'n/a')}",
        "",
        "RESUMO",
        f"- Nós: {snapshot['counts']['nodes']}",
        f"- Edges: {snapshot['counts']['edges']} | LBs: {snapshot['counts']['load_balancers']}",
        f"- Prontos: {snapshot['counts']['ready']} | Ativos: {snapshot['counts']['active']}",
        f"- Capacidade contratada: {snapshot['summary']['capacity_mbps']} Mbps",
        f"- Capacidade útil: {snapshot['summary']['usable_mbps']} Mbps",
        f"- Tráfego medido: {snapshot['summary']['tx_mbps']} Mbps",
        f"- Pressão máxima: {snapshot['summary']['pressure']}",
        f"- Nós sob pressão: {snapshot['summary']['pressured_nodes']}",
        f"- Nós em drenagem: {snapshot['summary']['draining_nodes']}",
        "",
        "NÓS",
    ]
    for item in snapshot["nodes"]:
        runtime = item.get("runtime") or {}
        lines.append(
            f"{item['node']['id']:>4} | {item['node']['name']:<24.24} | {item['node']['role']:<14} | "
            f"{item['node']['state']:<11} | {item.get('capacity_mbps') or '-':>5} Mbps | "
            f"{item.get('tx_mbps') or 0:>7.2f} Mbps | peso {runtime.get('applied_weight', '-')}"
        )
    message("\n".join(lines), height=28)


def choose_cluster_node(host):
    snapshot = cluster_snapshot(host)
    if not snapshot["nodes"]:
        message("Nenhum nó cadastrado no cluster.")
        return None, snapshot
    entries = []
    for item in snapshot["nodes"]:
        node = item["node"]
        label = f"{node['name']} | {node['ipv4']} | {node['role']} | {node['state']}"
        entries.append((node["id"], label))
    selected = choose("Selecionar VPS do cluster", entries, height=22)
    if selected is None:
        return None, snapshot
    return next((item for item in snapshot["nodes"] if item["node"]["id"] == selected), None), snapshot


def node_detail(host, item=None):
    if item is None:
        item, _ = choose_cluster_node(host)
    if item is None:
        return
    message(_format_capacity(item), height=28)


def local_sample_payload():
    role = "unknown"
    try:
        role = json.loads(Path("/etc/cdnmnus/node-role.json").read_text()).get("role", "unknown")
    except Exception:
        pass
    if role not in {"edge", "load_balancer"}:
        raise RuntimeError("amostragem local está disponível apenas em edge ou load balancer")
    route = subprocess.run(
        ["ip", "route", "get", "1.1.1.1"],
        capture_output=True, text=True, check=False,
    )
    if route.returncode != 0 or not route.stdout.strip():
        raise RuntimeError("não foi possível determinar a interface de saída")
    tokens = route.stdout.split()
    try:
        iface = tokens[tokens.index("dev") + 1]
    except (ValueError, IndexError):
        raise RuntimeError("interface de saída não identificada")

    def read_int(path):
        return int(Path(path).read_text().strip())

    def cpu_sample():
        first = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        time.sleep(0.5)
        second = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        total = sum(second) - sum(first)
        idle = second[3] - first[3]
        return 0.0 if total <= 0 else round((1 - idle / total) * 100, 2)

    def memory_sample():
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, rest = line.split(":", 1)
            values[key] = int(rest.strip().split()[0])
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        return 0.0 if not total else round(((total - available) / total) * 100, 2)

    def edge_health_sample():
        probes = []
        context = ssl._create_unverified_context()
        for scheme in ("https", "http"):
            request = f"{scheme}://127.0.0.1/edge-health"
            start = time.perf_counter()
            try:
                response = urllib.request.urlopen(
                    urllib.request.Request(request), timeout=2, context=context,
                )
                elapsed = (time.perf_counter() - start) * 1000
                if getattr(response, "status", 0) == 200:
                    probes.append(elapsed)
            except Exception:
                continue
        if not probes:
            return 0.0, False
        probes.sort()
        return round(probes[-1], 2), True

    tx_before = read_int(f"/sys/class/net/{iface}/statistics/tx_bytes")
    cpu_pct = cpu_sample()
    time.sleep(2)
    tx_after = read_int(f"/sys/class/net/{iface}/statistics/tx_bytes")
    p95_ms, health_ok = edge_health_sample()
    memory_pct = memory_sample()
    active_sessions = int(subprocess.run(
        ["sh", "-c", "ss -Htan state established | wc -l"], capture_output=True, text=True, check=False
    ).stdout.strip() or "0")
    nic_errors = read_int(f"/sys/class/net/{iface}/statistics/tx_errors") + read_int(f"/sys/class/net/{iface}/statistics/rx_errors")
    tx_mbps = round(max(0, tx_after - tx_before) * 8 / 2 / 1_000_000, 2)
    node_id = json.loads(Path("/etc/cdnmnus/node-role.json").read_text()).get("node_id")
    return {
        "node_id": node_id,
        "sampled_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "interface_name": iface,
        "tx_mbps": tx_mbps,
        "p95_ms": p95_ms,
        "http5xx": 0.0 if health_ok else 100.0,
        "active_sessions": active_sessions,
        "cpu_pct": cpu_pct,
        "mem_pct": memory_pct,
        "nic_errors": nic_errors,
        "vod_206_ok": health_ok,
        "sample_window_sec": 2,
    }


def submit_local_sample(host):
    payload = local_sample_payload()
    result = control_plane_json(
        host,
        "sudo -n /usr/local/sbin/cdnmnus-submit-capacity-sample",
        input_text=json.dumps(payload),
        timeout=120,
    )
    message(
        "AMOSTRA ENVIADA AO CONTROL PLANE\n\n"
        f"Nó: {result['node_id']}\n"
        f"Interface: {result['interface_name']}\n"
        f"Tráfego: {result['tx_mbps']} Mbps\n"
        f"CPU: {result['cpu_pct']}%\n"
        f"Memória: {result['mem_pct']}%\n"
        f"Sessões: {result['active_sessions']}\n"
        f"p95: {result['p95_ms']} ms\n"
        f"HTTP 5xx: {result['http5xx']}\n"
        f"VOD 206 OK: {'sim' if result['vod_206_ok'] else 'não'}"
    )


def submit_local_capacity_profile(host, node_id):
    capacity_raw = ask("Capacidade contratada em Mbps", "1000")
    if capacity_raw is None or not capacity_raw.strip():
        return
    headroom_raw = ask("Headroom", "0.25")
    if headroom_raw is None or not headroom_raw.strip():
        return
    connections_raw = ask("Conexões máximas", "0")
    if connections_raw is None or not connections_raw.strip():
        return
    source = ask("Fonte da capacidade", "manual") or "manual"
    confidence = ask("Confiança (manual/contracted/measured/derived)", "manual") or "manual"
    payload = {
        "node_id": node_id,
        "capacity_mbps": int(capacity_raw),
        "headroom": float(headroom_raw),
        "max_connections": int(connections_raw),
        "source": source,
        "confidence": confidence,
    }
    measured = ask("Capacidade medida opcional", "")
    if measured and measured.strip():
        payload["measured_mbps"] = int(measured)
    expires = ask("Validade opcional (ISO8601 UTC)", "")
    if expires and expires.strip():
        payload["expires_at"] = expires
    result = control_plane_json(
        host,
        "sudo -n /usr/local/sbin/cdnmnus-submit-capacity-profile",
        input_text=json.dumps(payload),
        timeout=120,
    )
    message(
        "PERFIL DE CAPACIDADE SALVO\n\n"
        f"Nó: {result['node_id']}\n"
        f"Capacidade: {result['capacity_mbps']} Mbps\n"
        f"Headroom: {result['headroom']}\n"
        f"Capacidade útil: {(result['capacity_mbps'] * (1 - result['headroom'])):.2f} Mbps\n"
        f"Fonte: {result['source']}\n"
        f"Confiança: {result['confidence']}"
    )


def capacity_menu(data, host):
    while True:
        entries = [
            ("1", "Visão consolidada do cluster"),
            ("2", "Detalhe de uma VPS do cluster"),
        ]
        if data.get("role") in {"edge", "load_balancer"}:
            entries.extend([
                ("3", "Atualizar amostra desta VPS"),
                ("4", "Definir perfil contratado desta VPS"),
            ])
        entries.append(("5", "Testar reprodução pelo CNAME DNS-only"))
        entries.append(("0", "Voltar"))
        action = choose("Capacidade e consumo", entries)
        if action in (None, "0"):
            return
        try:
            if action == "1":
                cluster_overview(host)
            elif action == "2":
                node_detail(host)
            elif action == "3":
                submit_local_sample(host)
            elif action == "4":
                submit_local_capacity_profile(host, data["node_id"])
            elif action == "5":
                cname_lab()
        except Exception as exc:
            message("Falha na rotina de capacidade:\n\n" + str(exc))


def cname_lab():
    """Executa o ensaio CNAME sem guardar credenciais no nó."""
    script = Path("/opt/cdnmnus/lab-player/scripts/test_playback_flow.py")
    if not script.is_file():
        raise RuntimeError("laboratório de playback não está instalado neste nó")
    cname = ask("Base CNAME DNS-only", "https://cnxt.vr766.com")
    canonical = ask("Base canônica do tenant", "https://tvbrasil.phpd77.com")
    direct = ask("Base direta de comparação", "http://38.46.223.77")
    username = ask("Usuário de laboratório")
    password = ask("Senha de laboratório", password=True)
    if not all((cname, canonical, direct, username, password)):
        return
    env = os.environ.copy()
    env.update({
        "PLAYER_BASE_CNAME": cname,
        "PLAYER_BASE_CDN": canonical,
        "PLAYER_BASE_DIRECT": direct,
        "PLAYER_USERNAME": username,
        "PLAYER_PASSWORD": password,
        "PLAYER_LATEST_PLAYLIST": "/opt/cdnmnus/lab-player/playlists/cname_latest.m3u8",
    })
    result = subprocess.run([sys.executable, str(script), "--cname"], env=env,
                            capture_output=True, text=True, timeout=900, check=False)
    output = (result.stdout or result.stderr).strip()
    message(("TESTE CNAME APROVADO" if result.returncode == 0 else "TESTE CNAME FALHOU") +
            f"\n\n{output[-12000:]}", height=30)


def request_node_onboarding(host):
    code, role = dialog(["--menu", "Papel inicial da nova máquina", "14", "82", "2",
                         "edge", "Cadastrar como Edge",
                         "load_balancer", "Cadastrar diretamente como LB candidate"])
    if code != 0:
        return
    name = ask("Nome amigável da nova máquina")
    ipv4 = ask("IPv4 público da nova máquina")
    port = ask("Porta SSH", "22")
    initial_user = ask("Usuário SSH inicial", "root")
    password = ask("Senha SSH inicial — usada uma vez e nunca armazenada", password=True)
    if None in (name, ipv4, port, initial_user, password):
        return
    payload = {
        "name": name.strip(), "ipv4": ipv4.strip(), "ssh_port": int(port),
        "initial_user": initial_user.strip(), "password": password, "role": role,
    }
    password = ""
    code, _ = dialog([
        "--yesno",
        f"Cadastrar {payload['name']} ({payload['ipv4']}:{payload['ssh_port']}) como {role}?\n\n"
        "O control plane fixará a host key por TOFU auditado, instalará a tag aprovada e "
        "nunca ativará um LB diretamente.",
        "15", "88",
    ])
    if code != 0:
        payload.clear()
        return
    try:
        result = control_plane_ssh(
            host, "sudo -n /usr/local/sbin/cdnmnus-submit-node-onboarding",
            json.dumps(payload), timeout=2100,
        )
    finally:
        payload.clear()
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "control plane recusou o onboarding")
    response = json.loads(result.stdout)
    deployment = response.get("deployment_id") or "não aplicável ao LB candidate"
    message(
        "NOVA MÁQUINA REGISTRADA\n\n"
        f"ID: {response['node_id']}\nPapel: {response['role']}\nEstado: {response['state']}\n"
        f"Pacote: {response['package_ref']}\nDeployment: {deployment}\n\n"
        "A senha não foi armazenada. LB permanece sem ativação até lock/fencing."
    )


def request_promotion(data, host):
    if data.get("role") != "edge" or data.get("state") != "ready":
        raise RuntimeError("somente uma edge ready pode solicitar preparação para LB")
    capabilities = data.get("capabilities", {})
    if not capabilities.get("load_balancer_candidate"):
        raise RuntimeError("nó sem capacidade declarada para LB")
    package = package_identity()
    code, mode = dialog(["--menu", "Papel solicitado (não ativa o LB)", "14", "82", "2",
                         "candidate", "Preparar como candidato",
                         "standby", "Preparar como standby"])
    if code != 0:
        return
    code, reason = dialog(["--inputbox", "Motivo auditável da solicitação", "10", "88"])
    if code != 0 or not reason.strip():
        return
    remote_arguments = [
        "sudo", "-n", "/usr/local/sbin/cdnmnus-submit-promotion-request",
        "--node-id", str(data["node_id"]), "--mode", mode,
        "--package-ref", package["ref"], "--package-commit", package["commit"],
        "--manifest-digest", package["manifest_digest"], "--reason", reason.strip(),
    ]
    remote_command = " ".join(shlex.quote(part) for part in remote_arguments)
    result = control_plane_ssh(host, remote_command, timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "control plane recusou a solicitação")
    response = json.loads(result.stdout)
    message("SOLICITAÇÃO REGISTRADA\n\n"
            f"ID: {response['request_id']}\nEstado: {response['state']}\n\n"
            "Nenhuma role foi alterada. Aguarde aprovação no control plane.")


def main():
    if os.geteuid() != 0:
        print("Execute como root via SSH.", file=sys.stderr); return 1
    data = identity(); host = control_plane_host()
    while True:
        header = (f"Nó {data['node_id']} — {data.get('name', '')}\n"
                  f"Função: {ROLE_LABEL.get(data['role'], data['role'])} | "
                  f"Estado: {STATE_LABEL.get(data['state'], data['state'])}")
        entries = [
                               "1", "Identidade e conexão com o plano de controle",
                               "2", "Situação dos serviços locais",
                               "3", "Capacidade, consumo e saúde do cluster",
                               "4", "Validar configuração Nginx/HAProxy",
                               "5", "Orientações para operações centralizadas",
                               "6", "Cadastrar nova máquina (Edge ou Load Balancer)",
                               "8", "Abrir menu do Control Plane (DNS/Cloudflare/XUI)",
        ]
        entries.extend(["7", "Promover esta Edge para Load Balancer (solicitar aprovação)"])
        entries.extend(["0", "Sair"])
        code, action = dialog(["--menu", header, "21", "92", str(len(entries) // 2), *entries])
        if code != 0 or action == "0": return 0
        if action == "1":
            message(header + f"\nPlano de controle: {host} ({connectivity(host)})")
        elif action == "2":
            names = ("nginx", "haproxy", "cdnmnus-token-broker.service", "cdnmnus-admin.service")
            message("SERVIÇOS LOCAIS\n\n" + "\n".join(f"{name}: {service(name)}" for name in names))
        elif action == "3":
            try:
                capacity_menu(data, host)
            except Exception as exc:
                message("Falha na operação de capacidade:\n\n" + str(exc))
        elif action == "4":
            checks = []
            for binary, args in (("nginx", ["nginx", "-t"]), ("haproxy", ["haproxy", "-c", "-f", "/etc/haproxy/haproxy.cfg"])):
                if subprocess.run(["sh", "-c", f"command -v {binary}"], capture_output=True).returncode == 0:
                    result = subprocess.run(args, capture_output=True, text=True, check=False)
                    checks.append(f"{binary}: {'APROVADO' if result.returncode == 0 else 'FALHOU'}")
            message("VALIDAÇÕES LOCAIS\n\n" + ("\n".join(checks) or "Nenhum proxy instalado neste papel."))
        elif action == "5":
            message("Alterações de função, DNS, promoção e implantação são autorizadas apenas pelo Control Plane.\n\n"
                    "Este cliente é read-only local e não cria estado paralelo nem promove nós sem lock/fencing.")
        elif action == "6":
            try:
                request_node_onboarding(host)
            except Exception as exc:
                message("CADASTRO RECUSADO\n\n" + str(exc))
        elif action == "7":
            try:
                request_promotion(data, host)
            except Exception as exc:
                message("SOLICITAÇÃO RECUSADA\n\n" + str(exc))
        elif action == "8":
            try:
                open_control_plane_menu(host)
            except Exception as exc:
                message("MENU DO CONTROL PLANE INDISPONÍVEL\n\n" + str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
