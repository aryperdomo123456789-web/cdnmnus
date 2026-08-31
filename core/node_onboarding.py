"""Bootstrap universal de edge/LB, sempre executado pelo control plane."""
from __future__ import annotations

import gc
import json
import os
import re
from pathlib import Path
from typing import Any

from core.db import Database, normalize_port
from core.deploy import queue_deployment
from core.edge_manager import (
    bootstrap_edge,
    converge_ssh_mesh,
    install_managed_node_package,
    scan_host_identity,
)
from core.topology import TopologyStore


APPROVED_RELEASE_PATH = Path("/etc/cdnmnus/managed-node-release.json")


def load_approved_release(path: str | Path = APPROVED_RELEASE_PATH) -> dict[str, str]:
    release_path = Path(path)
    stat = release_path.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022:
        raise PermissionError("release aprovada deve pertencer a root e não pode ser gravável por grupo/outros")
    data = json.loads(release_path.read_text(encoding="utf-8"))
    required = {"ref", "commit", "manifest_digest"}
    if not isinstance(data, dict) or not required.issubset(data):
        raise ValueError("contrato da release aprovada incompleto")
    if not re.fullmatch(r"v[0-9][A-Za-z0-9._-]*", str(data["ref"])):
        raise ValueError("tag aprovada inválida")
    if not re.fullmatch(r"[a-f0-9]{40}", str(data["commit"])):
        raise ValueError("commit aprovado inválido")
    if not re.fullmatch(r"[a-f0-9]{64}", str(data["manifest_digest"])):
        raise ValueError("digest aprovado inválido")
    return {key: str(data[key]) for key in required}


def onboard_node(database: Database, *, name: str, ipv4: str, ssh_port: int,
                 initial_user: str, password: str, role: str,
                 operator: str, control_plane: str,
                 approved_release: dict[str, str] | None = None,
                 key_dir: str | Path = "/etc/cdnmnus/ssh") -> dict[str, Any]:
    """Faz TOFU auditado, instala o pacote e registra o papel sem ativar LB."""
    role = role.strip().lower()
    if role not in {"edge", "load_balancer"}:
        raise ValueError("papel inicial deve ser edge ou load_balancer")
    if not name.strip() or len(name.strip()) > 128 or "\n" in name:
        raise ValueError("nome do nó inválido")
    if not password:
        raise ValueError("senha inicial vazia")
    port = normalize_port(ssh_port)
    release = approved_release or load_approved_release()
    database.initialize()
    topology = TopologyStore(database); topology.initialize()
    if database.rows("SELECT 1 FROM nodes WHERE ipv4=? OR name=?", (ipv4.strip(), name.strip())):
        raise ValueError("IP ou nome já cadastrado")
    node_id = database.reserve_node_id()
    identity = scan_host_identity(ipv4, port)
    bootstrap = None
    try:
        bootstrap = bootstrap_edge(
            ipv4, port, initial_user, password, identity.sha256, node_id, key_dir,
        )
    finally:
        password = ""
        del password
        gc.collect()
    install_managed_node_package(
        ipv4, port, node_id, name.strip(), role, control_plane,
        release["ref"], release["commit"], release["manifest_digest"], key_dir,
    )
    if role == "edge":
        database.add_edge(
            node_id, name, ipv4, port, bootstrap["ssh_user"],
            bootstrap["fingerprint"], "bootstrapping",
        )
        database.set_edge_state(
            node_id, "bootstrapping", operator=operator,
            reason="host key capturada duas vezes e fixada por TOFU automatizado",
            payload={"trust_mode": "automated_tofu", "fingerprint": bootstrap["fingerprint"],
                     "package_ref": release["ref"], "package_commit": release["commit"]},
        )
    else:
        topology.add_node(
            node_id, name, ipv4, "load_balancer", "candidate", operator,
            "cadastro direto de LB candidate com TOFU automatizado",
            {"trust_mode": "automated_tofu", "package_ref": release["ref"],
             "package_commit": release["commit"]},
            ssh_port=port, ssh_user=bootstrap["ssh_user"],
            host_key_sha256=bootstrap["fingerprint"],
        )
    try:
        converge_ssh_mesh(database.path, key_dir, control_plane)
    except Exception:
        if role == "edge":
            database.set_edge_state(
                node_id, "failed", operator="node-onboarding",
                reason="falha na convergência da malha SSH",
            )
        else:
            topology.transition_node(
                node_id, "failed", "node-onboarding",
                "falha na convergência da malha SSH",
            )
        raise
    if role == "edge":
        deployment = None
        if database.tenants(enabled_only=True):
            deployment = queue_deployment(database, target_edge_id=node_id)
        return {"node_id": node_id, "role": role, "state": "bootstrapping",
                "fingerprint": bootstrap["fingerprint"], "trust_mode": "automated_tofu",
                "deployment_id": deployment["deployment_id"] if deployment else None,
                "package_ref": release["ref"]}
    load_balancer = topology.add_load_balancer(
        "lb-" + node_id, node_id, None, operator,
        "LB direto registrado como candidate; ativação exige lock e fencing",
    )
    return {"node_id": node_id, "role": role, "state": load_balancer["state"],
            "fingerprint": bootstrap["fingerprint"], "trust_mode": "automated_tofu",
            "deployment_id": None, "package_ref": release["ref"]}
