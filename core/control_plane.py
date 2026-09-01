"""Resolução do host autoritativo do plano de controle."""
from __future__ import annotations

import json
import os
from pathlib import Path


DEFAULT_CONTROL_PLANE_HOST = "143.14.168.111"
CONTROL_PLANE_CONF = Path("/etc/cdnmnus/control-plane.conf")
NODE_ROLE_JSON = Path("/etc/cdnmnus/node-role.json")


class ControlPlaneIdentityError(ValueError):
    """Ação operacional tentou usar identidade implícita do control-plane."""


def _host_from_control_plane_conf(path: Path = CONTROL_PLANE_CONF) -> str | None:
    if not path.is_file():
        return None
    config: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
    host = config.get("CONTROL_PLANE_HOST", "").strip()
    return host or None


def _host_from_node_role(path: Path = NODE_ROLE_JSON) -> str | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    control_plane = data.get("control_plane")
    if not isinstance(control_plane, dict):
        return None
    host = str(control_plane.get("host", "")).strip()
    return host or None


def resolve_control_plane_host(explicit: str | None = None, *, require_explicit: bool = False) -> str:
    """Resolve o host do plano de controle sem depender de um IP congelado."""
    if explicit and explicit.strip():
        return explicit.strip()
    env_host = os.environ.get("CDNMNUS_CONTROL_PLANE", "").strip()
    if env_host:
        return env_host
    file_host = _host_from_control_plane_conf()
    if file_host:
        return file_host
    node_host = _host_from_node_role()
    if node_host:
        return node_host
    if require_explicit:
        raise ControlPlaneIdentityError(
            "control-plane não identificado; configure CDNMNUS_CONTROL_PLANE "
            "ou /etc/cdnmnus/control-plane.conf"
        )
    return DEFAULT_CONTROL_PLANE_HOST
