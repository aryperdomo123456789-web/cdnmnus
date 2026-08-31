#!/usr/bin/env python3
"""Converge a malha SSH do cluster com uma identidade Ed25519 por nó.

Executa no control plane como root. Chaves privadas nunca saem do nó de origem;
somente chaves públicas e host keys confirmadas são distribuídas.
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import os
import pwd
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.edge_manager import _known_hosts_line, scan_host_identity  # noqa: E402


START = "# BEGIN CDNMNUS SSH MESH"
END = "# END CDNMNUS SSH MESH"


@dataclass(frozen=True)
class Node:
    node_id: str
    host: str
    port: int
    control_key: Path | None
    expected_fingerprint: str
    local: bool = False


def managed_content(existing: str, lines: list[str]) -> str:
    """Substitui somente o bloco gerenciado e preserva entradas do operador."""
    kept: list[str] = []
    inside = False
    for line in existing.splitlines():
        if line == START:
            inside = True
            continue
        if line == END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    unique = list(dict.fromkeys(line.strip() for line in lines if line.strip()))
    return "\n".join([*kept, START, *unique, END, ""])


def run(command: list[str], *, input_text: str | None = None, timeout: int = 45) -> str:
    result = subprocess.run(
        command, input=input_text, capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"comando falhou: {command[0]}")
    return result.stdout


def remote_command(node: Node, command: str, *, timeout: int = 45) -> str:
    assert node.control_key is not None
    return run([
        "ssh", "-i", str(node.control_key), "-p", str(node.port),
        "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=/etc/cdnmnus/ssh/known_hosts",
        f"cdn-deploy@{node.host}", command,
    ], timeout=timeout)


def ensure_local_identity(node_id: str) -> str:
    try:
        account = pwd.getpwnam("cdn-deploy")
    except KeyError:
        run(["useradd", "--create-home", "--shell", "/bin/bash", "cdn-deploy"])
        account = pwd.getpwnam("cdn-deploy")
    ssh_dir = Path(account.pw_dir) / ".ssh"
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(ssh_dir, account.pw_uid, account.pw_gid)
    key = ssh_dir / "id_ed25519"
    if not key.exists():
        run(["runuser", "-u", "cdn-deploy", "--", "ssh-keygen", "-q", "-t", "ed25519",
             "-N", "", "-C", f"cdnmnus-mesh-{node_id}", "-f", str(key)])
    os.chmod(key, 0o600)
    os.chmod(key.with_suffix(".pub"), 0o644)
    sudoers = Path("/etc/sudoers.d/cdn-deploy")
    sudoers.write_text("cdn-deploy ALL=(root) NOPASSWD: ALL\n", encoding="utf-8")
    os.chmod(sudoers, 0o440)
    run(["visudo", "-cf", str(sudoers)])
    return key.with_suffix(".pub").read_text(encoding="utf-8").strip()


def ensure_remote_identity(node: Node) -> str:
    script = (
        "set -eu; "
        "id cdn-deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash cdn-deploy; "
        "install -d -o cdn-deploy -g cdn-deploy -m 700 /home/cdn-deploy/.ssh; "
        f"test -f /home/cdn-deploy/.ssh/id_ed25519 || sudo -u cdn-deploy ssh-keygen -q -t ed25519 -N '' -C {shlex.quote('cdnmnus-mesh-' + node.node_id)} -f /home/cdn-deploy/.ssh/id_ed25519; "
        "chown cdn-deploy:cdn-deploy /home/cdn-deploy/.ssh/id_ed25519*; "
        "chmod 600 /home/cdn-deploy/.ssh/id_ed25519; chmod 644 /home/cdn-deploy/.ssh/id_ed25519.pub; "
        "printf 'cdn-deploy ALL=(root) NOPASSWD: ALL\\n' > /etc/sudoers.d/cdn-deploy; "
        "chmod 440 /etc/sudoers.d/cdn-deploy; visudo -cf /etc/sudoers.d/cdn-deploy >/dev/null; "
        "cat /home/cdn-deploy/.ssh/id_ed25519.pub"
    )
    public = remote_command(node, f"sudo -n bash -c {shlex.quote(script)}").strip()
    if not public.startswith("ssh-ed25519 "):
        raise RuntimeError(f"nó {node.node_id} não apresentou chave pública Ed25519 válida")
    return public


def firewall_script(extra_control_port: bool = False) -> str:
    specs = [("22/tcp", "cdnmnus-ssh-public"), ("80/tcp", "cdnmnus-http"),
             ("443/tcp", "cdnmnus-https")]
    if extra_control_port:
        specs.append(("1455/tcp", "operational-1455"))
    commands = [
        "set -eu", "command -v ufw >/dev/null",
        "added=\"$(ufw show added)\"",
    ]
    for spec, comment in specs:
        commands.append(
            f"printf '%s\\n' \"$added\" | grep -Eq '^ufw allow {spec}([[:space:]]|$)' || "
            f"ufw allow {spec} comment {shlex.quote(comment)}"
        )
    commands.extend(["ufw --force enable", "systemctl enable ufw >/dev/null"])
    return "; ".join(commands)


def ensure_firewalls(nodes: list[Node]) -> None:
    for node in nodes:
        if node.local:
            # O control plane/futuro LB é gerido por seu role próprio, inclusive
            # a exceção 1455. O serviço da malha não amplia esse privilégio.
            continue
        script = firewall_script()
        remote_command(node, f"sudo -n bash -c {shlex.quote(script)}")


def install_local_file(path: Path, content: str) -> None:
    account = pwd.getpwnam("cdn-deploy")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.write(fd, content.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chown(temporary, account.pw_uid, account.pw_gid)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def install_remote_file(node: Node, name: str, content: str) -> None:
    encoded = base64.b64encode(content.encode()).decode()
    script = (
        "set -eu; install -d -o cdn-deploy -g cdn-deploy -m 700 /home/cdn-deploy/.ssh; "
        f"printf %s {shlex.quote(encoded)} | base64 -d > /home/cdn-deploy/.ssh/{name}.new; "
        f"chown cdn-deploy:cdn-deploy /home/cdn-deploy/.ssh/{name}.new; "
        f"chmod 600 /home/cdn-deploy/.ssh/{name}.new; "
        f"mv /home/cdn-deploy/.ssh/{name}.new /home/cdn-deploy/.ssh/{name}"
    )
    remote_command(node, f"sudo -n bash -c {shlex.quote(script)}")


def load_nodes(db_path: Path, key_dir: Path, control_host: str) -> list[Node]:
    control_host = str(ipaddress.ip_address(control_host))
    nodes = [Node("control-plane", control_host, 22, None, "", True)]
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    for row in db.execute("SELECT id,ipv4,ssh_port,host_key_sha256,state FROM edges ORDER BY id"):
        if row["state"] == "disabled":
            continue
        preferred = key_dir / f"{row['id']}.ed25519"
        candidates = [preferred] if preferred.is_file() else []
        candidates.extend(path for path in sorted(key_dir.glob("*.ed25519")) if path != preferred)
        key = next((path for path in candidates if control_key_authenticates(
            path, str(row["ipv4"]), int(row["ssh_port"]), key_dir / "known_hosts"
        )), None)
        if key is None:
            raise RuntimeError(f"nenhuma chave de controle autentica na edge {row['id']}")
        nodes.append(Node(str(row["id"]), str(row["ipv4"]), int(row["ssh_port"]),
                          key, str(row["host_key_sha256"])))
    if len(nodes) < 2:
        raise RuntimeError("nenhuma edge com chave de controle disponível")
    return nodes


def control_key_authenticates(key: Path, host: str, port: int, known_hosts: Path) -> bool:
    result = subprocess.run([
        "ssh", "-i", str(key), "-p", str(port), "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no", "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
        f"cdn-deploy@{host}", "true",
    ], capture_output=True, text=True, timeout=8, check=False)
    return result.returncode == 0


def read_remote_file(node: Node, name: str) -> str:
    return remote_command(node, f"sudo -n sh -c 'test ! -f /home/cdn-deploy/.ssh/{name} || cat /home/cdn-deploy/.ssh/{name}'")


def verify_mesh(nodes: list[Node]) -> None:
    for source in nodes:
        for target in nodes:
            if source == target:
                continue
            ssh = (
                f"ssh -p {target.port} -o BatchMode=yes -o PasswordAuthentication=no "
                f"-o StrictHostKeyChecking=yes -o ConnectTimeout=6 cdn-deploy@{target.host} true"
            )
            if source.local:
                run(["runuser", "-u", "cdn-deploy", "--", "bash", "-c", ssh], timeout=12)
            else:
                remote_command(source, f"bash -c {shlex.quote(ssh)}", timeout=12)


def converge(db_path: Path, key_dir: Path, control_host: str) -> None:
    if os.geteuid() != 0:
        raise PermissionError("a convergência da malha SSH exige root")
    nodes = load_nodes(db_path, key_dir, control_host)
    ensure_firewalls(nodes)
    public_keys: list[str] = []
    host_keys: list[str] = []
    for node in nodes:
        public_keys.append(ensure_local_identity(node.node_id) if node.local else ensure_remote_identity(node))
        identity = scan_host_identity(node.host, node.port)
        if node.expected_fingerprint and identity.sha256 != node.expected_fingerprint:
            raise PermissionError(f"fingerprint SSH mudou no nó {node.node_id}; convergência abortada")
        host_keys.append(_known_hosts_line(identity).strip())

    local_home = Path(pwd.getpwnam("cdn-deploy").pw_dir) / ".ssh"
    for name, lines in (("authorized_keys", public_keys), ("known_hosts", host_keys)):
        path = local_home / name
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        install_local_file(path, managed_content(existing, lines))
        for node in nodes:
            if node.local:
                continue
            existing = read_remote_file(node, name)
            install_remote_file(node, name, managed_content(existing, lines))
    verify_mesh(nodes)
    print(f"malha SSH convergida e validada: {len(nodes)} nós")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("/var/lib/cdnmnus-admin/admin.db"))
    parser.add_argument("--key-dir", type=Path, default=Path("/etc/cdnmnus/ssh"))
    parser.add_argument("--control-host", required=True)
    args = parser.parse_args()
    converge(args.db.resolve(), args.key_dir.resolve(), args.control_host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
