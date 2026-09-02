#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import core.edge_manager as mod

# Host key da edge aceita Ed25519, ECDSA ou RSA e prioriza Ed25519.
real_subprocess_run = mod.subprocess.run
fake_blob = "dmFsaWQtaG9zdC1rZXk="
mod.subprocess.run = lambda *args, **kwargs: SimpleNamespace(
    returncode=0,
    stdout=f"8.8.8.8 ssh-rsa {fake_blob}\n8.8.8.8 ssh-ed25519 {fake_blob}\n",
    stderr="",
)
scanned = mod.scan_host_identity("8.8.8.8", 22)
assert scanned.key_type == "ssh-ed25519" and scanned.sha256.startswith("SHA256:")
mod.subprocess.run = real_subprocess_run

identity = mod.HostIdentity("8.8.8.8", 22, "ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAITest", "SHA256:confirmed")
mod.scan_host_identity = lambda host, port: identity
seen = []
mod._run_ssh_password = lambda command, password, timeout=45: seen.append((command, password)) or "ok"
real_run = mod.subprocess.run
mod.subprocess.run = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="CDNMNUS_KEY_OK", stderr="")

with tempfile.TemporaryDirectory() as root:
    secret = "root-password-that-must-not-persist"
    result = mod.bootstrap_edge("8.8.8.8", 22, "root", secret, identity.sha256, "edge-a", root)
    private = Path(result["private_key"])
    assert private.is_file() and (private.stat().st_mode & 0o777) == 0o600
    assert "PRIVATE KEY" in private.read_text()
    assert secret not in private.read_text()
    assert secret not in Path(root, "known_hosts").read_text()
    assert secret not in " ".join(seen[0][0])
    try:
        mod.bootstrap_edge("8.8.8.8", 22, "root", secret, "SHA256:wrong", "edge-b", root)
        raise AssertionError("fingerprint divergente aceito")
    except PermissionError:
        pass

mod.subprocess.run = real_run
assert "NOPASSWD: ALL" not in (Path(mod.__file__).read_text())
assert (Path(mod.__file__).parents[1] / "scripts/cdnmnus-ansible-become").is_file()
print("edge bootstrap fingerprint/password checks: OK")
