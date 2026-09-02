"""Compilação de releases e chamada controlada do playbook serial."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import closing
import time
import uuid
from pathlib import Path
from typing import Any

from core.db import Database
from core.control_plane import resolve_control_plane_host
from core.render_tenants import broker_snapshot, render_all


def tenant_deployment_contexts(tenants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Constrói contextos fechados de deployment, um por tenant habilitado.

    Nenhuma propriedade de origem, health, VOD ou LB é compartilhada entre
    tenants. O resultado é serializável para Ansible e serve também como
    contrato de teste. O chamador deve preservar a lista inteira; selecionar
    um tenant específico exige um ID explícito.
    """
    contexts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tenant in tenants:
        tenant_id = str(tenant["id"])
        if tenant_id in seen:
            raise ValueError(f"tenant duplicado no deployment: {tenant_id}")
        seen.add(tenant_id)
        origins = [item for item in tenant.get("upstreams", []) if item["kind"] == "origin"]
        if len(origins) != 1:
            raise ValueError(f"tenant {tenant_id} precisa de exatamente uma origem")
        health_host = str(tenant.get("health_host") or tenant["canonical_host"])
        contexts.append({
            "tenant_id": tenant_id,
            "canonical_host": str(tenant["canonical_host"]),
            "health_host": health_host,
            "hosts": [str(item["hostname"]) for item in tenant.get("hosts", [])],
            "origin": {"host": origins[0]["host"], "port": origins[0]["port"]},
            "load_balancers": [
                {"host": item["host"], "port": item["port"]}
                for item in tenant.get("upstreams", []) if item["kind"] == "lb"
            ],
            "vod": [
                {"host": item["host"], "port": item["port"]}
                for item in tenant.get("upstreams", []) if item["kind"] == "vod"
            ],
            "certificate_dir": f"/etc/letsencrypt/live/{tenant['canonical_host']}",
        })
    return contexts


def _external_alias_context(db: Database, contexts: list[dict[str, Any]]) -> dict[str, Any]:
    """Obtém o tenant do alias externo por configuração explícita.

    A compatibilidade automática só é permitida quando há exatamente um
    tenant. Com múltiplos tenants, a ausência do ID é erro deliberado para
    impedir que um XUI herde a origem de outro.
    """
    configured_id = db.setting("external_alias_tenant_id")
    if configured_id:
        selected = [item for item in contexts if item["tenant_id"] == str(configured_id)]
        if len(selected) != 1:
            raise ValueError("external_alias_tenant_id não corresponde a tenant habilitado")
        return selected[0]
    if len(contexts) == 1:
        return next(iter(contexts))
    raise ValueError("múltiplos tenants exigem external_alias_tenant_id explícito")


def _ssh_key_for_edge(edge: dict[str, Any], key_dir: Path) -> Path:
    """Use numeric keys for new nodes and legacy inventory keys after migration."""
    numeric = key_dir / f"{edge['id']}.ed25519"
    if numeric.is_file():
        return numeric
    inventory = Path(__file__).resolve().parents[1] / "ansible/inventories/production/hosts.yml"
    if inventory.is_file():
        lines = inventory.read_text(encoding="utf-8", errors="ignore").splitlines()
        for index, line in enumerate(lines):
            if f"ansible_host: {edge['ipv4']}" not in line:
                continue
            for candidate in lines[index:index + 8]:
                match = re.search(r"ansible_ssh_private_key_file:\s*(\S+)", candidate)
                if match:
                    path = Path(match.group(1))
                    if path.is_file():
                        return path
    # Keep inventory generation compatible with dry-run/test fixtures that
    # intentionally use placeholder key paths; Ansible will validate access.
    return numeric


def _release_digest(files: dict[str, str]) -> str:
    """Digest canônico da lista fechada de artefatos da release."""
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_release(db: Database, release_root: str | Path = "/var/lib/cdnmnus-admin/releases") -> dict[str, Any]:
    tenants = db.tenants(enabled_only=True)
    if not tenants:
        raise ValueError("nenhum tenant habilitado")
    generation = max(int(item["config_version"]) for item in tenants)
    rendered = render_all(tenants)
    snapshot = broker_snapshot(tenants, generation)
    release_id = time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    root = Path(release_root)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    database_owner = db.path.stat()
    if os.geteuid() == 0:
        os.chown(root, database_owner.st_uid, database_owner.st_gid)
    os.chmod(root, 0o700)
    final = root / release_id
    with tempfile.TemporaryDirectory(prefix=".release-", dir=root) as temp_name:
        temp = Path(temp_name)
        (temp / "nginx/tenants").mkdir(parents=True)
        (temp / "broker").mkdir()
        (temp / "runtime").mkdir()
        hashes: dict[str, str] = {}
        for relative, item in rendered.items():
            destination = temp / "nginx" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(item.content, encoding="utf-8")
            os.chmod(destination, 0o640)
            hashes[str(destination.relative_to(temp))] = item.sha256
        (temp / "broker/tenants.json").write_text(snapshot, encoding="utf-8")
        os.chmod(temp / "broker/tenants.json", 0o640)
        hashes["broker/tenants.json"] = hashlib.sha256(snapshot.encode()).hexdigest()
        project_root = Path(__file__).resolve().parents[1]
        runtime_sources = {
            "runtime/token_broker.py": project_root / "panel/token_broker.py",
            "runtime/multi_tenant_broker.py": project_root / "panel/multi_tenant_broker.py",
            "runtime/vod_relay.py": project_root / "panel/vod_relay.py",
            "runtime/cdnmnus-tenant-broker@.service": project_root / "panel/cdnmnus-tenant-broker@.service",
            "runtime/cdnmnus-vod-relay@.service": project_root / "panel/cdnmnus-vod-relay@.service",
        }
        for relative, source in runtime_sources.items():
            if not source.is_file():
                raise FileNotFoundError(f"artefato obrigatório do runtime ausente: {source}")
            content = source.read_bytes()
            destination = temp / relative
            destination.write_bytes(content)
            os.chmod(destination, 0o640)
            hashes[relative] = hashlib.sha256(content).hexdigest()
        digest = _release_digest(hashes)
        manifest = {"schema_version": 1, "release_id": release_id, "generation": generation,
                    "tenant_count": len(tenants), "config_digest": digest, "files": hashes}
        (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp / "manifest.json", 0o640)
        # O menu é root, enquanto o worker roda como cdn-admin. A release deve
        # permanecer privada, mas legível pelo mesmo proprietário do banco.
        for path in [temp, *temp.rglob("*")]:
            if os.geteuid() == 0:
                os.chown(path, database_owner.st_uid, database_owner.st_gid)
            os.chmod(path, 0o750 if path.is_dir() else 0o640)
        os.chmod(temp, 0o700)
        # Mesmo filesystem: a release só se torna visível depois de completa.
        os.rename(temp, final)
    return {**manifest, "artifact_path": str(final)}


def _inventory(db: Database, key_dir: str | Path = "/etc/cdnmnus/ssh", *,
               include_bootstrapping: bool = False,
               edge_ids: set[str] | None = None) -> dict[str, Any]:
    hosts: dict[str, Any] = {}
    known_hosts = Path(key_dir) / "known_hosts"
    admitted_states = {"ready", "draining"}
    if include_bootstrapping:
        admitted_states.add("bootstrapping")
    for edge in db.edges():
        # ``bootstrapping`` só é admitido pelo pipeline completo de onboarding,
        # que executa preflight, ativação e auditoria antes de promover a ready.
        if edge["state"] not in admitted_states:
            continue
        if edge_ids is not None and edge["id"] not in edge_ids:
            continue
        key_path = _ssh_key_for_edge(edge, Path(key_dir))
        hosts[edge["id"]] = {
            "ansible_host": edge["ipv4"], "ansible_port": edge["ssh_port"],
            "ansible_user": "cdn-deploy", "ansible_become": True,
            "ansible_ssh_private_key_file": str(key_path),
            "ansible_ssh_common_args": f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_hosts}",
            "cdnmnus_node_id": edge["id"],
            "cdnmnus_node_name": edge["name"],
            "cdnmnus_node_role": "edge",
            "cdnmnus_node_state": "ready",
        }
        if db.setting("restricted_become_enabled", False):
            hosts[edge["id"]]["ansible_become_exe"] = "/usr/local/sbin/cdnmnus-ansible-become"
    if not hosts:
        raise ValueError("nenhuma edge elegível para este deployment")
    # Em um onboarding direcionado o inventário contém apenas o alvo. Ainda
    # assim, o hardening da edge precisa preservar a sessão do control plane;
    # sem esta fonte explícita o UFW poderia cortar o pipeline no momento da
    # ativação. Não abrir portas extras: somente autoriza o IP administrativo.
    return {
        "all": {
            "vars": {"cdnmnus_firewall_admin_sources": [resolve_control_plane_host()]},
            "children": {"cdn_edges": {"hosts": hosts}},
        }
    }


def queue_deployment(db: Database,
                     release_root: str | Path = "/var/lib/cdnmnus-admin/releases",
                     target_edge_id: str | None = None) -> dict[str, Any]:
    if target_edge_id is not None:
        target_edge = db.edge(target_edge_id)
        if target_edge["state"] != "bootstrapping":
            raise ValueError("o alvo explícito de onboarding deve estar em bootstrapping")
    release = build_release(db, release_root)
    deployment_id = "dep-" + uuid.uuid4().hex
    with closing(db.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO deployments(id,state,release_id,config_digest,artifact_path,target_edge_id) "
            "VALUES(?,?,?,?,?,?)",
            (deployment_id, "queued", release["release_id"], release["config_digest"],
             release["artifact_path"], target_edge_id),
        )
    return {"deployment_id": deployment_id, "state": "queued",
            "target_edge_id": target_edge_id, **release}


def claim_deployment(db: Database) -> dict[str, Any] | None:
    with db.transaction(immediate=True) as conn:
        row = conn.execute("SELECT * FROM deployments WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
        if row is None:
            return None
        changed = conn.execute("UPDATE deployments SET state='running',started_at=CURRENT_TIMESTAMP WHERE id=? AND state='queued'", (row["id"],))
        if changed.rowcount != 1:
            return None
        return dict(row)


def run_deployment(db: Database, deployment: dict[str, Any], inventory: str | Path | None = None,
                   playbook: str | Path = "ansible/playbooks/deploy-and-activate-edge.yml",
                   key_dir: str | Path = "/etc/cdnmnus/ssh") -> dict[str, Any]:
    if shutil.which("ansible-playbook") is None:
        error = "ansible-playbook não está instalado; instale ansible-core no control node"
        with closing(db.connect()) as conn, conn:
            conn.execute("UPDATE deployments SET state='failed',error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?", (error, deployment["id"]))
        raise RuntimeError(error)
    generated_inventory: Path | None = None
    managed_edge_ids: set[str] | None = None
    if inventory is None:
        fd, generated_name = tempfile.mkstemp(prefix="cdnmnus-inventory-", suffix=".json")
        generated_inventory = Path(generated_name)
        target_edge_id = deployment.get("target_edge_id")
        target_edge_ids = {str(target_edge_id)} if target_edge_id else None
        inventory_data = _inventory(
            db,
            key_dir,
            include_bootstrapping=bool(target_edge_id),
            edge_ids=target_edge_ids,
        )
        managed_edge_ids = set(
            inventory_data["all"]["children"]["cdn_edges"]["hosts"]
        )
        try:
            os.write(fd, json.dumps(inventory_data).encode())
        finally:
            os.close(fd)
        os.chmod(generated_inventory, 0o600)
        inventory = generated_inventory
    tenants = db.tenants(enabled_only=True)
    if not tenants:
        raise ValueError("nenhum tenant habilitado para ativação da edge")
    contexts = tenant_deployment_contexts(tenants)
    alias = _external_alias_context(db, contexts)
    control_plane_host = resolve_control_plane_host()
    command = ["ansible-playbook", "-i", str(inventory), str(playbook),
               "--extra-vars", json.dumps({
                   "release_id": deployment["release_id"],
                   "release_source": deployment["artifact_path"],
                   "config_digest": deployment["config_digest"],
                   "tenant_contexts": contexts,
                   "canonical_health_host": alias["health_host"],
                   "external_alias_tenant_id": alias["tenant_id"],
                   "external_alias_origin_host": alias["origin"]["host"],
                   "external_alias_origin_port": alias["origin"]["port"],
                   "external_alias_load_balancers": alias["load_balancers"],
                   "external_alias_has_vod": bool(alias["vod"]),
                   "tenant_ids": [item["tenant_id"] for item in contexts],
                   "vod_tenant_ids": [item["tenant_id"] for item in contexts if item["vod"]],
                   "tenant_health_hosts": [{"host": item["health_host"]} for item in contexts],
                   "cdnmnus_control_plane_host": control_plane_host,
               })]
    onboarding_edges = [
        edge for edge in db.edges()
        if edge["state"] == "bootstrapping"
        and managed_edge_ids is not None
        and edge["id"] in managed_edge_ids
    ]
    try:
        ansible_environment = os.environ.copy()
        ansible_environment["ANSIBLE_CONFIG"] = str(Path(__file__).resolve().parents[1] / "ansible/ansible.cfg")
        result = subprocess.run(
            command, cwd=Path(__file__).resolve().parents[1], env=ansible_environment,
            capture_output=True, text=True, timeout=3600, check=False,
        )
    finally:
        if generated_inventory is not None:
            generated_inventory.unlink(missing_ok=True)
    state = "succeeded" if result.returncode == 0 else "failed"
    if result.returncode == 0:
        error = None
    else:
        # O Ansible pode conter caminhos e nomes de hosts, mas não deve levar
        # argv/env com credenciais. Preserve também o erro fatal para não
        # transformar uma falha parcial em diagnóstico por tentativa e erro.
        output_lines = (result.stderr or result.stdout).splitlines()
        evidence = [line for line in output_lines
                    if re.search(r"\b(?:fatal|failed|msg|error)\b", line, re.IGNORECASE)]
        tail = output_lines[-8:]
        selected = evidence[-12:] + [line for line in tail if line not in evidence]
        error = "ansible-playbook falhou; resumo sanitizado:\n" + "\n".join(selected)[:4000]
    with closing(db.connect()) as conn, conn:
        conn.execute("UPDATE deployments SET state=?,error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                     (state, error, deployment["id"]))
    if result.returncode != 0:
        for edge in onboarding_edges:
            db.set_edge_state(
                edge["id"], "failed", operator="deployment-worker",
                reason="onboarding recusado por preflight, ativação ou auditoria",
                payload={"deployment_id": deployment["id"], "release_id": deployment["release_id"]},
            )
        raise RuntimeError(error or "deploy falhou")
    for edge in db.edges():
        selected = managed_edge_ids is None or edge["id"] in managed_edge_ids
        if edge["state"] == "bootstrapping" and managed_edge_ids is None:
            continue
        if selected and edge["state"] in ("bootstrapping", "ready", "draining"):
            target_state = "ready" if edge["state"] == "bootstrapping" else edge["state"]
            db.set_edge_state(
                edge["id"], target_state, deployment["release_id"],
                config_digest=deployment["config_digest"], operator="deployment-worker",
                reason="release verificada, ativada e auditada",
                payload={"deployment_id": deployment["id"], "release_id": deployment["release_id"]},
            )
    return {"deployment_id": deployment["id"], "state": state,
            "release_id": deployment["release_id"], "config_digest": deployment["config_digest"],
            "artifact_path": deployment["artifact_path"]}


def deploy_serial(db: Database, inventory: str | Path | None = None,
                  playbook: str | Path = "ansible/playbooks/deploy-edge.yml",
                  release_root: str | Path = "/var/lib/cdnmnus-admin/releases",
                  key_dir: str | Path = "/etc/cdnmnus/ssh") -> dict[str, Any]:
    queued = queue_deployment(db, release_root)
    claimed = claim_deployment(db)
    if claimed is None or claimed["id"] != queued["deployment_id"]:
        raise RuntimeError("deployment não pôde ser reservado")
    return run_deployment(db, claimed, inventory, playbook, key_dir)
