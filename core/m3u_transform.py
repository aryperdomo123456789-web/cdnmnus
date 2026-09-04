"""Transformação segura de playlists públicas para o canonical do tenant."""
from __future__ import annotations

import json
import io
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

try:
    from core.playlist_tokens import PlaylistTokenStore
except ImportError:  # runtime standalone da release
    from playlist_tokens import PlaylistTokenStore

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
SAFE_MEDIA_FRAGMENT = re.compile(r"^\.(?:aac|key|m4s|mkv|mp4|m3u8|ts)$", re.IGNORECASE)


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


def rewrite_public_playlist(playlist: str, snapshot: Mapping[str, Any], *, max_bytes: int = 1_048_576,
                            opaque_tokens: bool = False, token_ttl: int = 300,
                            token_store: PlaylistTokenStore | None = None,
                            collect_urls: bool = True) -> PlaylistTransformResult:
    playlist_size = len(playlist) if isinstance(playlist, bytes) else len(playlist.encode("utf-8"))
    if playlist_size > max_bytes:
        raise ValueError("playlist acima do limite permitido")
    header = playlist.lstrip() if isinstance(playlist, bytes) else playlist.lstrip().encode("utf-8")
    if not header.startswith(b"#EXTM3U"):
        raise ValueError("resposta não é uma playlist M3U")

    canonical, origin, extra = _snapshot_hosts(snapshot)
    allowed_hosts = {canonical, origin, *extra}
    rewritten_urls: list[str] = []
    rewritten_count = 0
    # A playlist pode ser grande. BytesIO evita que o resultado seja mantido
    # como uma segunda cópia UCS-2/4 antes de voltar a bytes no broker.
    output = io.BytesIO()
    tenant_id = str(snapshot.get("tenant_id") or snapshot.get("id") or "")
    token_store = (token_store or PlaylistTokenStore()) if opaque_tokens else None
    if token_store is not None:
        token_store.initialize()
    token_connection = token_store.connect() if token_store is not None else None

    def transform_url(value: str) -> str:
        nonlocal rewritten_count
        parsed = urlsplit(value)
        if parsed.scheme not in {"", "http", "https"}:
            raise ValueError("URL de mídia inválida")
        if parsed.fragment and not SAFE_MEDIA_FRAGMENT.fullmatch(parsed.fragment):
            raise ValueError("fragmento de mídia inválido")
        if parsed.scheme and not parsed.netloc:
            raise ValueError("URL de mídia inválida")
        host = normalize_hostname(parsed.hostname) if parsed.netloc else origin
        if host not in allowed_hosts:
            raise ValueError(f"playlist pública usa host fora do snapshot: {host}")
        path = parsed.path or "/"
        if not path.startswith("/"):
            if token_store is None:
                return value
            path = "/" + path
        if not path.startswith("/") or ".." in path.split("/"):
            raise ValueError("caminho de mídia inválido")
        # Some Xtream providers append the media extension as a fragment
        # (for example /play/<id>#.mp4). Fragments never reach the upstream,
        # but compatible players may rely on them locally.
        target = urlunsplit(("", "", path, parsed.query, ""))
        public_fragment = parsed.fragment
        if token_store is not None:
            if not tenant_id:
                raise ValueError("tenant ausente para tokenização")
            token = token_store.issue(tenant_id, host, target, ttl_seconds=token_ttl,
                                      connection=token_connection)
            rewritten_count += 1
            return urlunsplit(("https", canonical, f"/play/{token}", "", public_fragment))
        rewritten_count += 1
        return urlunsplit(("http", canonical, path, parsed.query, public_fragment))

    uri_attribute = re.compile(r"(?P<prefix>URI=)(?P<quote>[\"'])(?P<url>[^\"']+)(?P=quote)", re.IGNORECASE)
    try:
        source = io.BytesIO(playlist) if isinstance(playlist, bytes) else io.StringIO(playlist)
        for raw_value in source:
            raw_line = raw_value.decode("utf-8", "strict") if isinstance(raw_value, bytes) else raw_value
            line = raw_line.strip()
            if not line:
                output.write(raw_line.encode("utf-8"))
                continue
            if line.startswith("#"):
                def replace_attribute(match: re.Match[str]) -> str:
                    return f"{match.group('prefix')}{match.group('quote')}{transform_url(match.group('url'))}{match.group('quote')}"
                output.write(uri_attribute.sub(replace_attribute, raw_line).encode("utf-8"))
                continue
            parsed = urlsplit(line)
            if parsed.scheme not in {"", "http", "https"} or (parsed.scheme and not parsed.netloc):
                output.write(raw_line.encode("utf-8"))
                continue
            rewritten = transform_url(line)
            if collect_urls:
                rewritten_urls.append(rewritten)
            output.write((rewritten + ("\n" if raw_line.endswith("\n") else "")).encode("utf-8"))
    except Exception:
        if token_connection is not None:
            token_connection.rollback()
            token_connection.close()
        raise
    if token_connection is not None:
        token_connection.commit()
        token_connection.close()

    if not rewritten_count:
        raise ValueError("playlist M3U sem URLs de mídia")
    body = output.getvalue().decode("utf-8")
    return PlaylistTransformResult(
        body=body,
        canonical_host=canonical,
        rewritten_urls=tuple(rewritten_urls),
        sanitized_headers={},
    )


def rewrite_public_playlist_from_json(playlist: str, snapshot_json: str, **kwargs: Any) -> PlaylistTransformResult:
    return rewrite_public_playlist(playlist, json.loads(snapshot_json), **kwargs)
