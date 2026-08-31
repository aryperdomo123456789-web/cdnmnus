#!/usr/bin/env python3
"""Verifica a lista fechada do pacote universal antes de qualquer instalação."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"pacote universal inválido: {message}")


if len(sys.argv) != 5:
    fail("uso: verify.py ROOT MANIFEST REF COMMIT")
root = Path(sys.argv[1]).resolve()
manifest_path = Path(sys.argv[2]).resolve()
expected_ref, expected_commit = sys.argv[3:]
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("schema_version") != 1:
    fail("schema incompatível")
if manifest.get("source_ref") != expected_ref:
    fail("tag diverge da autorização")
try:
    actual_commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
except (OSError, subprocess.CalledProcessError):
    fail("pacote não está dentro do clone Git verificado")
if actual_commit != expected_commit:
    fail("commit do clone diverge da autorização")
files = manifest.get("files")
if not isinstance(files, dict) or not files:
    fail("lista de arquivos ausente")
for relative, expected in files.items():
    if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
        fail("caminho fora do pacote")
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        fail("hash malformado")
    path = root / relative
    if path.is_symlink() or not path.is_file():
        fail(f"arquivo obrigatório ausente: {relative}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        fail(f"hash divergente: {relative}")
print(json.dumps({"source_ref": expected_ref, "source_commit": expected_commit,
                  "file_count": len(files)}, sort_keys=True))
