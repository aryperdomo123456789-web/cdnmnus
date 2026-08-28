"""Descoberta de host key e bootstrap SSH sem persistir a senha inicial."""
from __future__ import annotations

import base64
import gc
import hashlib
import hmac
import ipaddress
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pexpect
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.db import normalize_port

USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class HostIdentity:
    host: str
    port: int
    key_type: str
    public_key: str
    sha256: str


def _public_ipv4(value: str) -> str:
    address = ipaddress.ip_address(value.strip())
    if address.version != 4 or not address.is_global:
        raise ValueError("a edge deve possuir IPv4 público global")
    return str(address)


def scan_host_identity(host: str, port: int = 22, timeout: int = 8) -> HostIdentity:
    host = _public_ipv4(host)
    port = normalize_port(port)
    if shutil.which("ssh-keyscan") is None:
        raise RuntimeError("ssh-keyscan não está instalado")
    result = subprocess.run(
        ["ssh-keyscan", "-T", str(timeout), "-p", str(port), "-t", "ed25519,ecdsa,rsa", host],
        capture_output=True, text=True, timeout=timeout + 2, check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    if not lines:
        try:
            with socket.create_connection((host, port), timeout=3):
                reachable = True
        except OSError:
            reachable = False
        if not reachable:
            raise RuntimeError(f"SSH inacessível em {host}:{port}; confira IP, porta e firewall")
        raise RuntimeError("SSH respondeu, mas não apresentou host key Ed25519, ECDSA ou RSA compatível")
    supported = {"ssh-ed25519": 0, "ecdsa-sha2-nistp256": 1, "ecdsa-sha2-nistp384": 2,
                 "ecdsa-sha2-nistp521": 3, "ssh-rsa": 4}
    parsed = [line.split() for line in lines]
    parsed = [fields for fields in parsed if len(fields) >= 3 and fields[1] in supported]
    if not parsed:
        raise RuntimeError("resposta de host key SSH inválida ou com algoritmo não autorizado")
    fields = min(parsed, key=lambda item: supported[item[1]])
    try:
        blob = base64.b64decode(fields[2], validate=True)
    except ValueError as exc:
        raise RuntimeError("host key SSH malformada") from exc
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return HostIdentity(host, port, fields[1], fields[2], fingerprint)


def _known_hosts_line(identity: HostIdentity) -> str:
    target = identity.host if identity.port == 22 else f"[{identity.host}]:{identity.port}"
    return f"{target} {identity.key_type} {identity.public_key}\n"


def _generate_keypair(comment: str) -> tuple[bytearray, str]:
    key = Ed25519PrivateKey.generate()
    private = bytearray(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ))
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode() + f" {comment}"
    del key
    return private, public


def _run_ssh_password(command: list[str], password: str, timeout: int = 45) -> str:
    """Executa SSH em PTY; a senha nunca integra argv, env, arquivo ou log."""
    child = pexpect.spawn(command[0], command[1:], encoding="utf-8", timeout=timeout, echo=False)
    child.logfile = None
    output: list[str] = []
    try:
        while True:
            match = child.expect([
                r"(?i)(?:password|senha).*:",
                r"CDNMNUS_BOOTSTRAP_OK",
                pexpect.EOF,
                pexpect.TIMEOUT,
            ])
            output.append(child.before or "")
            if match == 0:
                child.sendline(password)
            elif match == 1:
                output.append("CDNMNUS_BOOTSTRAP_OK")
            elif match == 2:
                break
            else:
                raise RuntimeError("timeout durante bootstrap SSH")
        child.close()
        if child.exitstatus != 0 or "CDNMNUS_BOOTSTRAP_OK" not in "".join(output):
            raise RuntimeError("bootstrap remoto falhou; confira usuário, senha e sudo")
        return "bootstrap concluído"
    finally:
        if child.isalive():
            child.close(force=True)


def bootstrap_edge(host: str, port: int, initial_user: str, password: str,
                   expected_fingerprint: str, edge_id: str,
                   key_dir: str | Path = "/etc/cdnmnus/ssh") -> dict[str, str]:
    """Cria cdn-deploy, instala a chave e comprova login sem senha."""
    if not password:
        raise ValueError("senha inicial vazia")
    if not USER_RE.fullmatch(initial_user):
        raise ValueError("usuário SSH inicial inválido")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", edge_id):
        raise ValueError("edge_id inválido")
    identity = scan_host_identity(host, port)
    if not expected_fingerprint or not hmac.compare_digest(expected_fingerprint, identity.sha256):
        raise PermissionError("fingerprint SSH diverge da confirmação do operador")

    private, public = _generate_keypair(f"cdnmnus-{edge_id}")
    key_root = Path(key_dir)
    key_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    private_path = key_root / f"{edge_id}.ed25519"
    known_hosts = key_root / "known_hosts"
    if private_path.exists():
        raise FileExistsError("já existe chave privada para esta edge")
    with tempfile.TemporaryDirectory(prefix="cdnmnus-bootstrap-") as temp_name:
        temp = Path(temp_name)
        temp_key = temp / "key"
        temp_known = temp / "known_hosts"
        temp_key.write_bytes(private)
        os.chmod(temp_key, 0o600)
        temp_known.write_text(_known_hosts_line(identity), encoding="utf-8")
        os.chmod(temp_known, 0o600)
        quoted_key = shlex.quote(public)
        script = (
            "set -eu; "
            "id cdn-deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash cdn-deploy; "
            "install -d -o cdn-deploy -g cdn-deploy -m 700 /home/cdn-deploy/.ssh; "
            f"printf '%s\\n' {quoted_key} > /home/cdn-deploy/.ssh/authorized_keys; "
            "chown cdn-deploy:cdn-deploy /home/cdn-deploy/.ssh/authorized_keys; "
            "chmod 600 /home/cdn-deploy/.ssh/authorized_keys; "
            "printf 'cdn-deploy ALL=(root) NOPASSWD: ALL\\n' > /etc/sudoers.d/cdn-deploy; "
            "chmod 440 /etc/sudoers.d/cdn-deploy; visudo -cf /etc/sudoers.d/cdn-deploy >/dev/null; "
            "echo CDNMNUS_BOOTSTRAP_OK"
        )
        remote = f"bash -c {shlex.quote(script)}" if initial_user == "root" else f"sudo bash -c {shlex.quote(script)}"
        command = ["ssh", "-p", str(identity.port), "-o", "PreferredAuthentications=password,keyboard-interactive",
                   "-o", "PubkeyAuthentication=no", "-o", "StrictHostKeyChecking=yes",
                   "-o", f"UserKnownHostsFile={temp_known}", f"{initial_user}@{identity.host}", remote]
        try:
            _run_ssh_password(command, password)
            check = subprocess.run(
                ["ssh", "-i", str(temp_key), "-p", str(identity.port), "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={temp_known}",
                 f"cdn-deploy@{identity.host}", "printf CDNMNUS_KEY_OK"],
                capture_output=True, text=True, timeout=20, check=False,
            )
            if check.returncode != 0 or check.stdout != "CDNMNUS_KEY_OK":
                raise RuntimeError("a conexão por chave após bootstrap não foi validada")
            fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, private)
                os.fsync(fd)
            finally:
                os.close(fd)
            key_owner = key_root.stat()
            os.chown(private_path, key_owner.st_uid, key_owner.st_gid)
            existing = known_hosts.read_text(encoding="utf-8") if known_hosts.exists() else ""
            if _known_hosts_line(identity) not in existing:
                with known_hosts.open("a", encoding="utf-8") as stream:
                    stream.write(_known_hosts_line(identity))
                os.chmod(known_hosts, 0o600)
            os.chown(known_hosts, key_owner.st_uid, key_owner.st_gid)
        finally:
            for index in range(len(private)):
                private[index] = 0
            password = ""
            del password
            gc.collect()
    return {"fingerprint": identity.sha256, "private_key": str(private_path), "ssh_user": "cdn-deploy"}
