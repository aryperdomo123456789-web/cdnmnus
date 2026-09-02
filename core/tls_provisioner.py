"""Provisionamento ACME por tenant com falha isolada.

O módulo executa no control-plane. A distribuição não é reimplementada aqui:
o único caminho remoto é ``scripts/distribute_tls.sh``, que delega validação,
instalação atômica e reload ao instalador já homologado.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

from core.db import Database, normalize_hostname


class TLSProvisionError(RuntimeError):
    """Falha em uma etapa do tenant, sem implicar falha de outros tenants."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _redact(text: str) -> str:
    text = text.replace("\n", " ").strip()
    if not text:
        return ""
    patterns = (
        r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----",
        r"(?i)\b(private[_ -]?key|token|credential|password|passwd|secret)\b[^ ]*",
        r"(?i)(authorization:)\s*\S+",
    )
    for pattern in patterns:
        text = re.sub(pattern, "[REDACTED]", text)
    return text[:600]


def log_json(event: str, **fields: Any) -> None:
    """Escreve auditoria sem chave privada, token ou credencial."""
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=os.sys.stderr)


class TLSProvisioner:
    """Orquestra emissão, SAN, distribuição e health de um único tenant."""

    def __init__(self, database: Database, *, runner: Runner = subprocess.run,
                 acme_helper: str = "/opt/cdnmnus/scripts/cdnmnus-acme-helper",
                 distribution_script: str = "/opt/cdnmnus/scripts/distribute_tls.sh",
                 live_root: str = "/etc/letsencrypt/live",
                 edge_ips: Sequence[str] = ("143.14.168.168", "143.14.168.170", "143.14.168.78")) -> None:
        self.database = database
        self.runner = runner
        self.acme_helper = acme_helper
        self.distribution_script = distribution_script
        self.live_root = Path(live_root)
        self.edge_ips = tuple(edge_ips)

    def stage_shared_certificate(self, tenant_id: str, *, source: str = "/var/lib/cdnmnus-admin/tls-source") -> callable:
        """Emite SAN para todos os tenants ativos e devolve rollback do arquivo-fonte."""
        candidate = self.database.tenant(tenant_id)
        tenants = self.database.tenants(enabled_only=True)
        if all(item["id"] != tenant_id for item in tenants):
            tenants.append(candidate)
        hosts = sorted({str(host["hostname"]).lower() for item in tenants for host in item.get("hosts", [])})
        canonical = str(self.database.setting("managed_canonical_host", "cdn.phpd77.com"))
        sans = sorted({canonical, *hosts})
        action = "renew" if (self.live_root / canonical / "fullchain.pem").is_file() else "issue"
        result = self._run(
            ["sudo", self.acme_helper, "--action", action, "--canonical", canonical,
             "--sans", ",".join(sans), "--tenant-id", "shared"],
            stage="shared-acme", timeout=900,
        )
        try:
            payload = json.loads(result.stdout.strip())
            lineage = Path(str(payload["lineage"])).resolve()
            if lineage != (self.live_root / canonical).resolve():
                raise TLSProvisionError("lineage compartilhada divergente")
        except (KeyError, json.JSONDecodeError) as exc:
            raise TLSProvisionError("resposta compartilhada ACME inválida") from exc
        self._validate_sans(lineage, sans)
        target = Path(source)
        target.mkdir(mode=0o750, parents=True, exist_ok=True)
        files = {name: target / name for name in ("fullchain.pem", "privkey.pem")}
        backups: dict[Path, bytes | None] = {path: path.read_bytes() if path.exists() else None for path in files.values()}
        for name, path in files.items():
            fd, staged_name = tempfile.mkstemp(prefix=f".{name}.", dir=target)
            os.close(fd)
            staged = Path(staged_name)
            try:
                shutil.copyfile(lineage / name, staged)
                os.chmod(staged, 0o640)
                os.replace(staged, path)
            finally:
                staged.unlink(missing_ok=True)

        def rollback() -> None:
            for path, content in backups.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
                    os.chmod(path, 0o640)
        return rollback

    def _tenant_hosts(self, tenant_id: str) -> tuple[dict[str, Any], list[str]]:
        tenant = self.database.tenant(tenant_id)
        hosts = [normalize_hostname(str(item["hostname"])) for item in tenant.get("hosts", [])]
        if not hosts:
            raise TLSProvisionError("tenant sem hosts publicados")
        return tenant, list(dict.fromkeys(hosts))

    def _run(self, argv: list[str], *, stage: str, timeout: int) -> subprocess.CompletedProcess[str]:
        log_json("tls_stage_started", stage=stage, command=argv[0])
        try:
            result = self.runner(argv, text=True, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            reason = f"{stage} timeout after {timeout}s"
            log_json("tls_stage_failed", stage=stage, reason=reason)
            raise TLSProvisionError(reason) from exc
        if result.returncode != 0:
            output = _redact(result.stderr or result.stdout or "")
            reason = f"{stage} falhou: {output or 'saída indisponível'}"
            log_json("tls_stage_failed", stage=stage, reason=reason)
            raise TLSProvisionError(reason)
        log_json("tls_stage_succeeded", stage=stage)
        return result

    def _issue(self, tenant: dict[str, Any], hosts: list[str]) -> Path:
        canonical = normalize_hostname(str(tenant["canonical_host"]))
        action = "renew" if (self.live_root / canonical / "fullchain.pem").exists() else "issue"
        argv = ["sudo", self.acme_helper, "--action", action,
                "--canonical", canonical, "--sans", ",".join(hosts),
                "--tenant-id", str(tenant["id"])]
        result = self._run(argv, stage="acme", timeout=900)
        try:
            reported = json.loads(result.stdout.strip())
            if not isinstance(reported, dict):
                raise TLSProvisionError("resposta ACME não é um objeto JSON")
            if reported.get("tenant_id") != str(tenant["id"]):
                raise TLSProvisionError("helper ACME retornou tenant divergente")
            if normalize_hostname(str(reported.get("canonical", ""))) != canonical:
                raise TLSProvisionError("helper ACME retornou lineage/canonical divergente")
            reported_lineage = Path(str(reported.get("lineage", "")))
            if reported_lineage.resolve() != (self.live_root / canonical).resolve():
                raise TLSProvisionError("helper ACME retornou lineage divergente")
        except json.JSONDecodeError as exc:
            raise TLSProvisionError("resposta inválida do helper ACME") from exc
        lineage = self.live_root / canonical
        if not (lineage / "fullchain.pem").is_file() or not (lineage / "privkey.pem").is_file():
            raise TLSProvisionError(f"lineage ACME incompleta: {lineage}")
        return lineage

    def _validate_sans(self, lineage: Path, hosts: list[str]) -> dict[str, Any]:
        result = self._run(["openssl", "x509", "-in", str(lineage / "fullchain.pem"),
                            "-noout", "-ext", "subjectAltName"], stage="san", timeout=60)
        names = {match.rstrip(".").lower() for match in re.findall(
            r"DNS:([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+)",
            result.stdout,
        )}
        missing = [host for host in hosts if host not in names and not any(
            name.startswith("*.") and host.endswith(name[1:]) for name in names)]
        if missing:
            raise TLSProvisionError("SAN ausente: " + ", ".join(missing))
        return {"san": sorted(names), "hosts": hosts}

    def _health(self, tenant: dict[str, Any], hosts: list[str]) -> dict[str, Any]:
        health_host = tenant.get("health_host")
        if not health_host:
            raise TLSProvisionError("tenant sem health_host explícito")
        health_host = normalize_hostname(str(health_host))
        checks: dict[str, Any] = {}
        for edge_ip in self.edge_ips:
            stage = f"health:{edge_ip}"
            argv = [
                "curl", "--resolve", f"{health_host}:443:{edge_ip}",
                "--silent", "--show-error", "--http2",
                "--connect-timeout", "10", "--max-time", "15",
                "--output", "/dev/null", "--write-out", "%{http_code}",
                f"https://{health_host}/edge-health",
            ]
            log_json("tls_stage_started", stage=stage, command=argv[0])
            try:
                result = self.runner(argv, text=True, capture_output=True, timeout=30, check=False)
            except subprocess.TimeoutExpired as exc:
                reason = f"{stage} timeout after 30s"
                log_json("tls_stage_failed", stage=stage, reason=reason)
                raise TLSProvisionError(reason) from exc
            if result.returncode != 0:
                stderr = _redact(result.stderr or "")
                if result.returncode == 60:
                    reason = f"{stage} SAN/TLS verification failed para {health_host}"
                elif result.returncode == 7:
                    reason = f"{stage} conexão recusada para {health_host}"
                elif result.returncode == 28:
                    reason = f"{stage} timeout para {health_host}"
                else:
                    reason = f"{stage} falhou com curl exit {result.returncode}"
                if stderr:
                    reason = f"{reason}: {stderr}"
                log_json("tls_stage_failed", stage=stage, reason=reason)
                raise TLSProvisionError(reason)
            http_code_text = (result.stdout or "").strip()
            try:
                http_code = int(http_code_text)
            except ValueError as exc:
                reason = f"{stage} resposta HTTP inválida"
                log_json("tls_stage_failed", stage=stage, reason=reason)
                raise TLSProvisionError(reason) from exc
            if http_code != 200:
                if http_code == 421:
                    reason = f"{stage} HTTP 421 para {health_host}"
                elif 400 <= http_code < 600:
                    reason = f"{stage} HTTP {http_code} para {health_host}"
                else:
                    reason = f"{stage} HTTP inesperado {http_code} para {health_host}"
                log_json("tls_stage_failed", stage=stage, reason=reason)
                raise TLSProvisionError(reason)
            checks[edge_ip] = {"status": "healthy", "http_status": http_code, "health_host": health_host}
            log_json("tls_stage_succeeded", stage=stage)
        return checks

    def provision(self, tenant_id: str, *, job_id: str | None = None,
                  lease_id: str | None = None) -> dict[str, Any]:
        """Executa o pipeline inteiro e altera somente o tenant informado."""
        if (job_id is None) != (lease_id is None):
            raise ValueError("job_id e lease_id devem ser informados juntos")
        tenant, hosts = self._tenant_hosts(tenant_id)
        try:
            lineage = self._issue(tenant, hosts)
            evidence = self._validate_sans(lineage, hosts)
            self._run([self.distribution_script, str(lineage)], stage="distribution", timeout=1200)
            evidence["edges"] = self._health(tenant, hosts)
            updated = self.database.set_tls_status(
                tenant_id, "valid", operator="tls-provisioner",
                reason="ACME, SAN, distribuição e health aprovados", evidence=evidence,
                job_id=job_id, lease_id=lease_id,
            )
            return {"tenant_id": tenant_id, "status": "valid", "hosts": hosts,
                    "edges": evidence["edges"], "tenant": updated}
        except Exception as exc:
            reason = _redact(str(exc))
            log_json("tls_provision_failed", tenant_id=tenant_id, reason=reason[:500])
            try:
                self.database.set_tls_status(
                    tenant_id, "failed", operator="tls-provisioner",
                    reason=reason[:512], evidence={"hosts": hosts},
                    job_id=job_id, lease_id=lease_id,
                )
            except ValueError as status_exc:
                if job_id is None or "não está mais sob posse" not in str(status_exc):
                    raise
            raise TLSProvisionError(reason) from exc

    def verify_staged(self, tenant_id: str,
                      source: str = "/var/lib/cdnmnus-admin/tls-source") -> dict[str, Any]:
        """Verifica o certificado staged e as edges sem emitir outro ACME.

        O onboarding já emitiu o SAN compartilhado antes do deployment. Reemitir
        aqui introduziria uma dependência desnecessária de permissões na árvore
        privada do Certbot e poderia consumir a cota ACME.
        """
        tenant, hosts = self._tenant_hosts(tenant_id)
        lineage = Path(source)
        fullchain = lineage / "fullchain.pem"
        privkey = lineage / "privkey.pem"
        if not fullchain.is_file() or not privkey.is_file():
            raise TLSProvisionError("certificado staged incompleto")
        evidence = self._validate_sans(lineage, hosts)
        evidence["edges"] = self._health(tenant, hosts)
        self.database.set_tls_status(
            tenant_id, "valid", operator="tls-provisioner",
            reason="certificado staged, SAN e health aprovados", evidence=evidence,
        )
        return {"tenant_id": tenant_id, "status": "valid", **evidence}
