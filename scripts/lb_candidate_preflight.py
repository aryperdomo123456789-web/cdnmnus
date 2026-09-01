#!/usr/bin/env python3
"""Audita um load balancer candidato sem alterar estado ou tráfego.

O comando valida release, processos, configuração, TLS, edges, capacidade,
leases e DNS/VIP para um nó já registrado no TopologyStore. Todas as ações
remotas são comandos de leitura via SSH. O processo nunca executa promoção,
reload/restart, escrita no banco, escrita no Cloudflare ou alteração de DNS.

Schema do relatório JSON:
    node, role, state, traffic_enabled, haproxy_config, nginx_config,
    release, tls, backends, capacity, lease, fencing, dns_vip,
    promotion_allowed, checks e errors.

Rollback: não há rollback necessário porque o comando é estritamente
observacional. Se qualquer gate não puder ser provado, a promoção continua
bloqueada e o erro é incluído no relatório.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("CDNMNUS_PROJECT_ROOT", "/opt/cdnmnus")).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import Database  # noqa: E402
from core.cloudflare_dns import CloudflareDNS, CloudflareError  # noqa: E402
from core.db import normalize_hostname  # noqa: E402
from core.node_onboarding import load_approved_release  # noqa: E402
from core.topology import TopologyStore  # noqa: E402


class PreflightError(RuntimeError):
    """Indica que uma evidência obrigatória não pôde ser obtida."""


def log_json(event: str, **fields: Any) -> None:
    """Emite uma linha de auditoria JSON sem credenciais ou conteúdo de mídia."""
    record = {"event": event, **fields}
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def ssh_command(node: dict[str, Any], command: str, timeout: int) -> str:
    """Executa somente o comando read-only solicitado no nó identificado."""
    key = node.get("ssh_key") or f"/etc/cdnmnus/ssh/{node['id']}.ed25519"
    user = node.get("ssh_user") or "cdn-deploy"
    port = int(node.get("ssh_port") or 22)
    host_key = "/etc/cdnmnus/ssh/known_hosts"
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={host_key}",
        "-i", key, "-p", str(port), f"{user}@{node['ipv4']}", command,
    ]
    result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout + 5, check=False)
    if result.returncode != 0:
        raise PreflightError(f"ssh {node['ipv4']} falhou: {result.stderr[-300:].strip()}")
    return result.stdout.strip()


def remote_probe(node: dict[str, Any], command: str, timeout: int) -> str:
    """Executa probe remoto sem shell mutável e registra somente o resultado."""
    log_json("remote_probe_started", node=node["ipv4"], check=command.split()[0])
    value = ssh_command(node, command, timeout)
    log_json("remote_probe_finished", node=node["ipv4"], check=command.split()[0], ok=True)
    return value


def command_status(node: dict[str, Any], command: str, timeout: int) -> tuple[bool, str]:
    """Retorna sucesso e saída curta para comandos locais do host candidato."""
    try:
        return True, remote_probe(node, command, timeout)
    except (OSError, subprocess.SubprocessError, PreflightError) as exc:
        return False, str(exc)


def check_release(node: dict[str, Any], approved: dict[str, str], timeout: int) -> dict[str, Any]:
    """Compara package.json remoto com a release aprovada pelo control-plane."""
    command = "sudo -n python3 -c " + shlex.quote(
        "import json, pathlib; "
        "p=pathlib.Path('/var/lib/cdnmnus-node/package.json'); "
        "r=pathlib.Path('/opt/cdnmnus/current').resolve(); "
        "m=r/'manifest.json'; "
        "print(json.dumps({'package': json.loads(p.read_text()), 'active_path': str(r), "
        "'active_manifest': json.loads(m.read_text()) if m.is_file() else None}))"
    )
    try:
        observed = json.loads(remote_probe(node, command, timeout))
        installed = observed.get("package", {})
        fields = {key: installed.get(key) == approved[key] for key in approved}
        manifest = observed.get("active_manifest") or {}
        manifest_fields = {
            "release_id_present": bool(manifest.get("release_id")),
            "config_digest_present": bool(manifest.get("config_digest")),
        }
        return {"installed": {key: installed.get(key) for key in approved}, "approved": approved,
                "active_path": observed.get("active_path"), "active_manifest": manifest,
                "matches": all(fields.values()), "fields": fields,
                "manifest_checks": manifest_fields}
    except (json.JSONDecodeError, PreflightError, OSError, subprocess.SubprocessError) as exc:
        return {"matches": False, "error": str(exc)}


def check_config(node: dict[str, Any], timeout: int) -> tuple[str, str | None]:
    """Valida HAProxy e Nginx sem reload, restart ou alteração de arquivos."""
    haproxy_ok, haproxy_output = command_status(node, "sudo -n haproxy -c -f /etc/haproxy/haproxy.cfg", timeout)
    nginx_ok, nginx_output = command_status(
        node, "if command -v nginx >/dev/null; then sudo -n nginx -t; else printf not_applicable; fi", timeout
    )
    nginx_state = "not_applicable" if nginx_output.strip() == "not_applicable" else ("valid" if nginx_ok else "invalid")
    return ("valid" if haproxy_ok else "invalid"), nginx_state


def check_service(node: dict[str, Any], service: str, timeout: int) -> str | None:
    """Lê o estado systemd sem iniciar, parar, habilitar ou desabilitar serviços."""
    ok, output = command_status(node, f"systemctl is-active {shlex.quote(service)} || true", timeout)
    return output.strip() if ok else None


def check_certificate(node: dict[str, Any], public_host: str, timeout: int) -> dict[str, Any]:
    """Inspeciona SANs dos PEMs referenciados pelo HAProxy candidato."""
    ok, config = command_status(node, "sudo -n sed -n '/^[[:space:]]*bind .* crt /p' /etc/haproxy/haproxy.cfg", timeout)
    if not ok:
        return {"valid": False, "error": config}
    paths = []
    for line in config.splitlines():
        if " crt " in line:
            paths.append(line.split(" crt ", 1)[1].split()[0])
    if not paths:
        return {"valid": False, "error": "nenhum PEM referenciado pelo HAProxy"}
    san_output = []
    for path in paths:
        valid, output = command_status(node, f"sudo -n openssl x509 -in {shlex.quote(path)} -noout -ext subjectAltName", timeout)
        if not valid:
            return {"valid": False, "paths": paths, "error": output}
        san_output.append(output)
    names = set()
    for output in san_output:
        for item in output.replace("\n", " ").split(","):
            item = item.strip()
            if item.startswith("DNS:"):
                names.add(item[4:].strip().rstrip("." ).lower())
    covered = public_host.rstrip(".").lower() in names or any(
        name.startswith("*.") and public_host.lower().endswith(name[1:]) for name in names
    )
    return {"valid": covered, "paths": paths, "san": sorted(names), "required_host": public_host,
            "covers_required_host": covered}


def check_backend(ipv4: str, health_host: str, timeout: int) -> dict[str, Any]:
    """Executa o equivalente a curl --resolve e classifica a falha observada."""
    argv = [
        "curl", "--resolve", f"{health_host}:443:{ipv4}",
        "--silent", "--show-error", "--output", "/dev/null",
        "--write-out", "%{http_code}", "--connect-timeout", str(timeout),
        "--max-time", str(timeout), f"https://{health_host}/edge-health",
    ]
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout + 2, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unhealthy", "reason": "probe_error", "detail": str(exc)}
    code = result.stdout.strip()
    if result.returncode == 0 and code == "200":
        return {"status": "healthy", "http_status": 200, "probe": "curl --resolve"}
    reason = "http_status_code"
    if result.returncode == 7:
        reason = "connection_refused"
    elif result.returncode in {28, 52, 55, 56}:
        reason = "timeout"
    elif result.returncode == 35 or result.returncode == 60:
        reason = "tls_mismatch"
    return {"status": "unhealthy", "reason": reason, "http_status": int(code) if code.isdigit() else None,
            "curl_exit": result.returncode, "detail": result.stderr[-300:].strip()}


def check_dns_vip(db: Database, node_ip: str) -> dict[str, Any]:
    """Verifica DNS local e Cloudflare somente por listagem, nunca por mutação."""
    records = db.dns_records()
    local_hits = [row["hostname"] for row in records if row.get("target_ip") == node_ip]
    configured_vip = os.environ.get("CDNMNUS_PUBLIC_VIP", "").strip()
    vip_hit = configured_vip == node_ip
    result = {"verified": not local_hits and not vip_hit, "local_records_pointing_to_node": local_hits,
              "configured_vip_points_to_node": vip_hit, "external_provider": "cloudflare",
              "records_checked": False, "node_ip_found": False}
    try:
        provider = CloudflareDNS()
        names = {row["hostname"] for row in records}
        names.update(tenant["canonical_host"] for tenant in db.tenants(enabled_only=True))
        external = [record for name in sorted(names) for record in provider.records(name)]
        hits = [record for record in external if str(record.get("content", "")) == node_ip]
        result["records_checked"] = True
        result["node_ip_found"] = bool(hits)
        result["external_records_checked"] = len(external)
        result["verified"] = result["verified"] and not hits
        if hits:
            result["reason"] = "external DNS points to candidate"
    except (CloudflareError, OSError, ValueError) as exc:
        result["verified"] = False
        result["reason"] = "external DNS could not be verified"
        result["error"] = str(exc)
    return result


def check_capacity(topology: TopologyStore, node_id: str) -> dict[str, Any]:
    """Valida capacidade, origem, confiança e expiração do perfil declarado."""
    profile = topology.capacity_profile(node_id)
    if not profile:
        return {"declared": False, "capacity_mbps": None, "expired": False, "error": "profile_missing"}
    expires_at = profile.get("expires_at")
    expired = False
    if expires_at:
        try:
            expired = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except ValueError:
            expired = True
    valid_confidence = profile.get("confidence") in {"contracted", "measured", "manual"}
    declared = bool(int(profile.get("capacity_mbps", 0)) > 0 and profile.get("source") and valid_confidence and not expired)
    return {"declared": declared, "capacity_mbps": profile.get("capacity_mbps"),
            "confidence": profile.get("confidence"), "source": profile.get("source"),
            "expired": expired, "profile": profile}


def check_traffic(node: dict[str, Any], state: str, timeout: int) -> dict[str, Any]:
    """Confirma ausência de HAProxy ativo, processo e listeners 80/443."""
    command = "sudo -n sh -c " + shlex.quote(
        "printf '%s\\n' '[systemd]'; systemctl is-active haproxy || true; "
        "printf '%s\\n' '[process]'; pgrep -a haproxy || true; "
        "printf '%s\\n' '[listeners]'; ss -ltnp || true"
    )
    ok, output = command_status(node, command, timeout)
    if not ok:
        return {"traffic_enabled": True, "verified": False, "error": output}
    lines = output.splitlines()
    process_lines = lines[lines.index("[process]") + 1:lines.index("[listeners]")] if "[process]" in lines and "[listeners]" in lines else []
    listener_lines = lines[lines.index("[listeners]") + 1:] if "[listeners]" in lines else []
    systemd = lines[lines.index("[systemd]") + 1] if "[systemd]" in lines and len(lines) > lines.index("[systemd]") + 1 else "unknown"
    haproxy_listener = any((":80" in line or ":443" in line) and "haproxy" in line for line in listener_lines)
    process_running = bool(process_lines)
    disabled = state != "active" and systemd in {"inactive", "failed", "dead", "disabled"} and not process_running and not haproxy_listener
    return {"traffic_enabled": not disabled, "verified": disabled, "systemd": systemd,
            "haproxy_process_running": process_running, "haproxy_listener": haproxy_listener}


def build_report(db: Database, node_ip: str, *, health_host: str | None = None,
                 timeout: int = 8) -> dict[str, Any]:
    """Monta o relatório completo; qualquer gate ausente mantém promoção bloqueada."""
    topology = TopologyStore(db)
    rows = db.rows("SELECT * FROM nodes WHERE ipv4=?", (node_ip,))
    if not rows:
        raise PreflightError(f"nó não registrado: {node_ip}")
    node = dict(rows[0])
    if node["role"] != "load_balancer":
        raise PreflightError(f"nó {node_ip} não possui role load_balancer")
    tenants = db.tenants(enabled_only=True)
    if not tenants:
        raise PreflightError("nenhum tenant habilitado para definir health_host")
    health_hosts = [normalize_hostname(str(tenant.get("health_host") or tenant["canonical_host"]))
                    for tenant in tenants]
    if health_host:
        health_hosts = [normalize_hostname(health_host)]
    lbs = db.rows("SELECT * FROM load_balancers WHERE node_id=?", (node["id"],))
    lb = lbs[0] if lbs else None
    approved = load_approved_release()
    release = check_release(node, approved, timeout)
    haproxy_config, nginx_config = check_config(node, timeout)
    backend_ips = [row["ipv4"] for row in db.rows("""SELECT n.ipv4 FROM lb_backends b JOIN nodes n ON n.id=b.edge_node_id
                                      WHERE b.load_balancer_id=? AND b.state='enabled' ORDER BY n.ipv4""",
                                    (lb["id"],) if lb else ("",))]
    backend_diagnostics = {
        ip: {"status": "healthy", "probes": [{"health_host": host, **check_backend(ip, host, timeout)}
                                                for host in health_hosts]}
        for ip in backend_ips
    }
    for diagnostic in backend_diagnostics.values():
        diagnostic["status"] = "healthy" if all(probe["status"] == "healthy" for probe in diagnostic["probes"]) else "unhealthy"
    backends = {ip: diagnostic["status"] for ip, diagnostic in backend_diagnostics.items()}
    capacity = check_capacity(topology, node["id"])
    locks = db.rows("SELECT * FROM promotion_locks")
    dns_vip = check_dns_vip(db, node_ip)
    tls = check_certificate(node, health_hosts[0], timeout)
    traffic = check_traffic(node, node["state"], timeout)
    security = {
        "state_standby": node["state"] == "standby",
        "lease_null": node.get("lease_id") is None and not locks,
        "traffic_disabled": traffic["verified"],
        "dns_vip_clear": dns_vip["verified"],
    }
    fencing = "not_configured"
    gates = [release.get("matches", False), haproxy_config == "valid", nginx_config != "invalid",
             bool(backends) and all(value["status"] == "healthy" for value in backend_diagnostics.values()), capacity["declared"],
             tls.get("valid", False), bool(lb and lb["mode"] == "active_standby"),
             all(security.values()), fencing == "configured"]
    errors = []
    if not release.get("matches"):
        errors.append("release instalada não corresponde à release aprovada")
    if haproxy_config != "valid":
        errors.append("configuração HAProxy inválida ou não verificável")
    if nginx_config == "invalid":
        errors.append("configuração Nginx inválida ou não verificável")
    if not backends or any(value["status"] != "healthy" for value in backend_diagnostics.values()):
        errors.append("uma ou mais edges não responderam health 200 com TLS/SNI")
    if not capacity["declared"]:
        errors.append("capacidade do LB não declarada")
    if not tls.get("valid", False):
        errors.append("certificado TLS não cobre o hostname público")
    if not security["traffic_disabled"]:
        errors.append("não foi possível provar que o HAProxy está inativo")
    if fencing != "configured":
        errors.append("fencing externo não configurado")
    report = {
        "schema": "cdnmnus.lb_candidate_preflight.v1",
        "node": node_ip, "node_id": node["id"], "role": node["role"], "state": node["state"],
        "traffic_enabled": traffic["traffic_enabled"],
        "haproxy_config": haproxy_config, "nginx_config": nginx_config,
        "release": release, "tls": tls,
        "health_hosts": health_hosts,
        "backends": backends, "backend_diagnostics": backend_diagnostics,
        "capacity": capacity, "lease": None if not locks else "present",
        "fencing": fencing, "dns_vip": dns_vip, "security": security,
        "traffic": traffic,
        "promotion_allowed": all(gates), "checks": {"all_gates_passed": all(gates)},
        "errors": errors,
    }
    report["checks"]["gate_count"] = len(gates)
    report["checks"]["passed_count"] = sum(bool(gate) for gate in gates)
    return report


def main(argv: list[str] | None = None) -> int:
    """Executa auditoria e imprime exclusivamente o relatório JSON em stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, help="IPv4 do LB candidato")
    parser.add_argument("--db", default=os.environ.get("CDNMNUS_ADMIN_DB", "/var/lib/cdnmnus-admin/admin.db"))
    parser.add_argument("--health-host", "--public-host", dest="health_host", default=None,
                        help="hostname de health; por padrão usa o canonical do tenant")
    parser.add_argument("--timeout", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        db = Database(args.db)
        report = build_report(db, args.node, health_host=args.health_host, timeout=args.timeout)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if report["promotion_allowed"] else 2
    except (OSError, ValueError, PreflightError) as exc:
        log_json("candidate_preflight_failed", node=args.node, error=str(exc))
        print(json.dumps({"schema": "cdnmnus.lb_candidate_preflight.v1", "node": args.node,
                          "promotion_allowed": False, "errors": [str(exc)]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
