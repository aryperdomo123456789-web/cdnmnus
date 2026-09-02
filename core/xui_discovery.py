"""Descobre destinos de mídia de uma M3U autorizada sem persistir segredos."""
from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from core.db import normalize_hostname

MAX_PLAYLIST_BYTES = 128 * 1024 * 1024
MAX_SAMPLES_PER_GROUP = 3
MAX_REDIRECTS = 3


@dataclass(frozen=True)
class XUIDiscoveryResult:
    load_balancers: tuple[str, ...]
    vod_seeds: tuple[str, ...]
    sampled_live: int
    sampled_vod: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _public_host(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("a M3U contém URL de mídia inválida")
    host = parsed.hostname.rstrip(".").lower()
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        host = normalize_hostname(host)
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None)}
        except OSError as exc:
            raise ValueError(f"não foi possível resolver destino de mídia: {host}") from exc
    if not addresses or not all(address.is_global for address in addresses):
        raise ValueError("destino de mídia não é público")
    return host


def _download_playlist(url: str, opener: urllib.request.OpenerDirector) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL M3U deve usar HTTP(S) e possuir hostname")
    request = urllib.request.Request(url, headers={"User-Agent": "cdnmnus-xui-discovery/1"})
    try:
        with opener.open(request, timeout=45) as response:
            data = response.read(MAX_PLAYLIST_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ValueError("não foi possível baixar a M3U autorizada") from exc
    if len(data) > MAX_PLAYLIST_BYTES:
        raise ValueError("M3U acima do limite operacional de 128 MiB")
    try:
        playlist = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("M3U não está em UTF-8") from exc
    if not playlist.lstrip().startswith("#EXTM3U"):
        raise ValueError("resposta não é uma playlist M3U")
    return playlist


def _entries(playlist: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    lines = [line.strip() for line in playlist.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        lower = line.lower()
        group = "live"
        if any(word in lower for word in ("movie", "filme", "vod")):
            group = "movie"
        elif any(word in lower for word in ("series", "serie")):
            group = "series"
        for candidate in lines[index + 1:]:
            if candidate and not candidate.startswith("#"):
                result.append((group, candidate))
                break
    return result


def _first_redirect(url: str, opener: urllib.request.OpenerDirector) -> str:
    current = url
    for _ in range(MAX_REDIRECTS):
        request = urllib.request.Request(current, method="GET", headers={"User-Agent": "cdnmnus-xui-discovery/1"})
        try:
            with opener.open(request, timeout=20) as response:
                return _public_host(response.geturl())
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise ValueError("amostra de mídia recusada pela origem") from exc
            location = exc.headers.get("Location")
            if not location:
                raise ValueError("redirecionamento de mídia sem destino") from exc
            current = urllib.parse.urljoin(current, location)
    raise ValueError("redirecionamento de mídia excedeu o limite")


def discover_xui_media(m3u_url: str) -> XUIDiscoveryResult:
    """Baixa a M3U e testa até nove amostras, retornando somente hosts públicos."""
    parsed = urllib.parse.urlsplit(m3u_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL M3U deve usar HTTP(S) e possuir hostname")
    opener = urllib.request.build_opener(_NoRedirect())
    entries = _entries(_download_playlist(m3u_url.strip(), opener))
    selected: list[tuple[str, str]] = []
    for group in ("live", "movie", "series"):
        selected.extend([item for item in entries if item[0] == group][:MAX_SAMPLES_PER_GROUP])
    if not selected:
        raise ValueError("M3U sem conteúdos de mídia")
    lbs: set[str] = set()
    vod: set[str] = set()
    for group, media_url in selected:
        host = _first_redirect(media_url, opener)
        (lbs if group == "live" else vod).add(host)
    return XUIDiscoveryResult(tuple(sorted(lbs)), tuple(sorted(vod)),
                              sum(group == "live" for group, _ in selected),
                              sum(group != "live" for group, _ in selected))
