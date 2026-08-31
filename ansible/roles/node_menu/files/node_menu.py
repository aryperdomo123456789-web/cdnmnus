#!/usr/bin/env python3
"""Cliente local comum; diagnóstico sem criar uma fonte de verdade paralela."""
import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path

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
    command = [
        "ssh", "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
        "-o", "StrictHostKeyChecking=yes", f"cdn-deploy@{host}",
        remote_command,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
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
                               "3", "Validar configuração Nginx/HAProxy",
                               "4", "Orientações para operações centralizadas",
        ]
        if data.get("role") == "edge" and data.get("state") == "ready":
            entries.extend(["5", "Solicitar preparação para Load Balancer"])
        entries.extend(["0", "Sair"])
        code, action = dialog(["--menu", header, "21", "92", str(len(entries) // 2), *entries])
        if code != 0 or action == "0": return 0
        if action == "1":
            message(header + f"\nPlano de controle: {host} ({connectivity(host)})")
        elif action == "2":
            names = ("nginx", "haproxy", "cdnmnus-token-broker.service", "cdnmnus-admin.service")
            message("SERVIÇOS LOCAIS\n\n" + "\n".join(f"{name}: {service(name)}" for name in names))
        elif action == "3":
            checks = []
            for binary, args in (("nginx", ["nginx", "-t"]), ("haproxy", ["haproxy", "-c", "-f", "/etc/haproxy/haproxy.cfg"])):
                if subprocess.run(["sh", "-c", f"command -v {binary}"], capture_output=True).returncode == 0:
                    result = subprocess.run(args, capture_output=True, text=True, check=False)
                    checks.append(f"{binary}: {'APROVADO' if result.returncode == 0 else 'FALHOU'}")
            message("VALIDAÇÕES LOCAIS\n\n" + ("\n".join(checks) or "Nenhum proxy instalado neste papel."))
        elif action == "4":
            message("Alterações de função, DNS, promoção e implantação são autorizadas apenas pelo Control Plane.\n\n"
                    "Este cliente é read-only local e não cria estado paralelo nem promove nós sem lock/fencing.")
        elif action == "5":
            try:
                request_promotion(data, host)
            except Exception as exc:
                message("SOLICITAÇÃO RECUSADA\n\n" + str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
