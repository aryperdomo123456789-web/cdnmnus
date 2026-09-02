"""Transformação segura de playlists públicas para o canonical do tenant."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

try:
    from core.db import normalize_hostname
except ImportError:  # runtime standalone da release
    def normalize_hostname(value: str) -> str:
        raw = value.strip().rstrip(".")
        if not raw or len(raw.encode("idna")) > 253:
            raise ValueError("hostname inválido")
        labels = raw.encode("idna").decode("ascii").lower().split(".")
        if any(not label or len(label) > 63 for label in labels):
            raise ValueError("hostname inválido")
        return ".".join(labels)

FORBIDDEN_HEADERS = {
    "location",
    "server",
    "set-cookie",
    "via",
    "x-powered-by",
    "x-accel-redirect",
}


@dataclass(frozen=True)
class PlaylistTransformResult:
    body: str
    canonical_host: str
    rewritten_urls: tuple[str, ...]
    sanitized_headers: dict[str, str]


def _snapshot_hosts(snapshot: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    canonical = normalize_hostname(str(snapshot["canonical_host"]))
    origin = normalize_hostname(str(snapshot["origin_host"]))
    extra: set[str] = set()
    for key in ("load_balancers", "vod_hosts"):
        for item in snapshot.get(key, []):
            host = item["host"] if isinstance(item, Mapping) else item
            extra.add(normalize_hostname(str(host)))
    return canonical, origin, tuple(sorted(extra))


def sanitize_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in FORBIDDEN_HEADERS or key.lower().startswith(("x-cdn-", "x-upstream-")):
            continue
        sanitized[key] = value
    return sanitized


def rewrite_public_playlist(playlist: str, snapshot: Mapping[str, Any], *, max_bytes: int = 1_048_576) -> PlaylistTransformResult:
    if len(playlist.encode("utf-8")) > max_bytes:
        raise ValueError("playlist acima do limite permitido")
    if not playlist.lstrip().startswith("#EXTM3U"):
        raise ValueError("resposta não é uma playlist M3U")

    canonical, origin, extra = _snapshot_hosts(snapshot)
    allowed_hosts = {canonical, origin, *extra}
    rewritten_urls: list[str] = []
    output_lines: list[str] = []
    for raw_line in playlist.splitlines():
        line = raw_line.strip()
        if not line:
            output_lines.append(raw_line)
            continue
        parsed = urlsplit(line)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            output_lines.append(raw_line)
            continue
        host = normalize_hostname(parsed.hostname or "")
        if host not in allowed_hosts:
            raise ValueError(f"playlist pública usa host fora do snapshot: {host}")
        rewritten = urlunsplit(("http", canonical, parsed.path or "/", parsed.query, ""))
        rewritten_urls.append(rewritten)
        output_lines.append(rewritten)

    if not rewritten_urls:
        raise ValueError("playlist M3U sem URLs de mídia")
    body = "\n".join(output_lines)
    if playlist.endswith("\n"):
        body += "\n"
    return PlaylistTransformResult(
        body=body,
        canonical_host=canonical,
        rewritten_urls=tuple(rewritten_urls),
        sanitized_headers={},
    )


def rewrite_public_playlist_from_json(playlist: str, snapshot_json: str, **kwargs: Any) -> PlaylistTransformResult:
    return rewrite_public_playlist(playlist, json.loads(snapshot_json), **kwargs)
