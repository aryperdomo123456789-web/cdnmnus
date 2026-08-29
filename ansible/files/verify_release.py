#!/usr/bin/env python3
"""Valida integralmente uma release CDNMenus, sem confiar no digest declarado."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

RELEASE_RE = re.compile(r"^[0-9]{14}-[a-f0-9]{8}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def fail(message: str) -> None:
    raise SystemExit(f"release inválida: {message}")


def main() -> None:
    if len(sys.argv) not in (2, 4):
        fail("uso: verify_release.py DIRETORIO [RELEASE_ID CONFIG_DIGEST]")
    root = Path(sys.argv[1]).resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"manifest.json ilegível: {exc}")

    required = {"schema_version", "release_id", "generation", "tenant_count", "config_digest", "files"}
    if not required.issubset(manifest):
        fail("campos obrigatórios ausentes no manifesto")
    if manifest["schema_version"] != 1:
        fail("schema_version não suportada")
    if not isinstance(manifest["release_id"], str) or not RELEASE_RE.fullmatch(manifest["release_id"]):
        fail("release_id malformado")
    if not isinstance(manifest["config_digest"], str) or not SHA256_RE.fullmatch(manifest["config_digest"]):
        fail("config_digest malformado")
    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        fail("lista de arquivos vazia ou inválida")

    actual_paths: set[str] = set()
    calculated: dict[str, str] = {}
    for relative, expected in sorted(files.items()):
        if not isinstance(relative, str) or not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            fail("entrada de arquivo malformada")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or relative in ("", "manifest.json"):
            fail(f"caminho inseguro no manifesto: {relative!r}")
        path = root / candidate
        if path.is_symlink() or not path.is_file():
            fail(f"arquivo ausente ou não regular: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            fail(f"hash divergente: {relative}")
        actual_paths.add(candidate.as_posix())
        calculated[candidate.as_posix()] = digest

    present_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "SYNCED"}
    }
    if present_paths != actual_paths:
        fail("conteúdo da release difere da lista fechada do manifesto")
    digest = hashlib.sha256(
        json.dumps(calculated, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != manifest["config_digest"]:
        fail("config_digest recalculado diverge do manifesto")
    if len(sys.argv) == 4:
        if manifest["release_id"] != sys.argv[2] or digest != sys.argv[3]:
            fail("identidade da release diverge do rollout solicitado")
    for directory, dirs, filenames in os.walk(root):
        for name in dirs + filenames:
            if (Path(directory) / name).is_symlink():
                fail("links simbólicos não são permitidos dentro da release")
    print(json.dumps({"release_id": manifest["release_id"], "config_digest": digest}, sort_keys=True))


if __name__ == "__main__":
    main()
