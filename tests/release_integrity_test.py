#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from core.db import Database
from core.deploy import _inventory, build_release


VERIFY = Path(__file__).parents[1] / "ansible/files/verify_release.py"
TENANT_TASKS = Path(__file__).parents[1] / "ansible/roles/cdn_tenants/tasks/main.yml"
ROLLBACK_PLAYBOOK = Path(__file__).parents[1] / "ansible/playbooks/rollback-edge.yml"
AUDIT_PLAYBOOK = Path(__file__).parents[1] / "ansible/playbooks/audit-edge-releases.yml"


def verify(path: Path, release: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(VERIFY), str(path), str(release["release_id"]), str(release["config_digest"])],
        text=True,
        capture_output=True,
        check=False,
    )


with tempfile.TemporaryDirectory() as temp_name:
    root = Path(temp_name)
    db = Database(root / "admin.db")
    db.initialize()
    db.add_tenant("xui1", "XUI", "xui.cdn.test", "origin.test")
    first = build_release(db, root / "releases")
    second = build_release(db, root / "releases")
    assert first["config_digest"] == second["config_digest"]
    release_path = Path(str(first["artifact_path"]))
    assert release_path.stat().st_uid == db.path.stat().st_uid
    assert (release_path.stat().st_mode & 0o777) == 0o700
    assert (release_path / "manifest.json").stat().st_uid == db.path.stat().st_uid
    assert (release_path / "manifest.json").stat().st_mode & 0o777 == 0o640
    assert verify(release_path, first).returncode == 0
    assert (release_path / "runtime/multi_tenant_broker.py").is_file()
    assert "runtime/multi_tenant_broker.py" in first["files"]
    assert (release_path / "runtime/vod_relay.py").is_file()
    assert "runtime/vod_relay.py" in first["files"]
    assert (release_path / "runtime/cdnmnus-vod-relay@.service").is_file()
    for unit_name in (
        "cdnmnus-tenant-broker@.service",
        "cdnmnus-vod-relay@.service",
    ):
        unit = (release_path / "runtime" / unit_name).read_text(encoding="utf-8")
        assert "/opt/cdnmnus/current/runtime/" in unit
        assert "/opt/cdnmnus/runtime/" not in unit

    tenant_file = release_path / "nginx/tenants/xui1.conf"
    original = tenant_file.read_text(encoding="utf-8")
    tenant_file.write_text(original + "# drift\n", encoding="utf-8")
    assert verify(release_path, first).returncode != 0
    tenant_file.write_text(original, encoding="utf-8")

    (release_path / "unexpected.conf").write_text("extra", encoding="utf-8")
    assert verify(release_path, first).returncode != 0
    (release_path / "unexpected.conf").unlink()

    manifest_path = release_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["../escape"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert verify(release_path, first).returncode != 0

    # Rollout normal exclui bootstrap; somente o onboarding completo o admite.
    with db.connect() as conn, conn:
        for edge_id, state, address in (
            ("pending1", "pending", "192.0.2.10"),
            ("bootstrap1", "bootstrapping", "192.0.2.11"),
            ("ready1", "ready", "192.0.2.12"),
            ("draining1", "draining", "192.0.2.13"),
            ("failed1", "failed", "192.0.2.14"),
        ):
            conn.execute(
                """INSERT INTO edges(id,name,ipv4,ssh_port,ssh_user,
                   host_key_sha256,state) VALUES(?,?,?,?,?,?,?)""",
                (edge_id, edge_id, address, 22, "cdn-deploy", "fixture", state),
            )
    hosts = _inventory(db, root / "ssh")["all"]["children"]["cdn_edges"]["hosts"]
    assert set(hosts) == {"ready1", "draining1"}
    onboarding_hosts = _inventory(
        db, root / "ssh", include_bootstrapping=True
    )["all"]["children"]["cdn_edges"]["hosts"]
    assert set(onboarding_hosts) == {"bootstrap1", "ready1", "draining1"}
    assert onboarding_hosts["bootstrap1"]["cdnmnus_node_role"] == "edge"
    assert onboarding_hosts["bootstrap1"]["cdnmnus_node_state"] == "ready"

    rollback_tasks = TENANT_TASKS.read_text(encoding="utf-8")
    assert "Preservar conteúdo das units anteriores para rollback" in rollback_tasks
    assert "Restaurar conteúdo das units que existiam" in rollback_tasks
    assert rollback_tasks.index("Restaurar release anterior atomicamente") < rollback_tasks.index(
        "Iniciar exatamente os brokers do snapshot anterior"
    )
    assert "Registrar contrato fechado de rollback da ativação" in rollback_tasks
    assert "/var/lib/cdnmnus-edge/activation-history/{{ release_id }}/rollback.json" in rollback_tasks
    assert "Definir se existe rollback anterior íntegro" in rollback_tasks
    assert "previous_manifest_stat.stat.exists" in rollback_tasks
    assert "previous_snapshot_stat.stat.exists" in rollback_tasks
    assert "when: not (previous_release_valid | bool)" in rollback_tasks

    explicit_rollback = ROLLBACK_PLAYBOOK.read_text(encoding="utf-8")
    assert "Recusar rollback para release diferente da preservada" in explicit_rollback
    assert "Restaurar unit anterior do broker" in explicit_rollback
    assert "Remover unit do relay ausente no estado anterior" in explicit_rollback
    assert explicit_rollback.index("Reapontar current atomicamente para a release anterior") < explicit_rollback.index(
        "Validar Nginx antes de recarregar o rollback"
    )
    assert "Restaurar symlink da candidata após falha no rollback" in explicit_rollback

    audit_tasks = AUDIT_PLAYBOOK.read_text(encoding="utf-8")
    assert "Derivar hosts de health do snapshot ativo" in audit_tasks
    assert 'loop: "{{ audit_health_hosts }}"' in audit_tasks
    assert 'loop: "{{ tenant_health_hosts }}"' not in audit_tasks
    assert "Exigir relay VOD ativo para cada tenant com VOD" in audit_tasks
    assert "Validar health privado de cada relay VOD" in audit_tasks

    onboarding = (Path(__file__).parents[1] / "ansible/playbooks/deploy-and-activate-edge.yml").read_text(
        encoding="utf-8"
    )
    assert onboarding.index("preflight-edge.yml") < onboarding.index("deploy-edge.yml")
    assert onboarding.index("activate-edge.yml") < onboarding.index("audit-edge-releases.yml")
    assert onboarding.index("audit-edge-releases.yml") < onboarding.index(
        "finalize-edge-onboarding.yml"
    )

print("release integrity checks: OK")
