#!/usr/bin/env python3
"""Validador isolado de reprodução para laboratório CDNMNUS."""

from __future__ import annotations

import json
import os
import sys
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
import socket

LAB_DIR = Path(os.environ.get("LAB_DIR", "/opt/cdnmnus/lab-player"))
USER_AGENT = os.environ.get("PLAYER_USER_AGENT", "IBOPlayerPro/3.6.0 (Android TV; ExoPlayerLib/2.18.7)")
TIMEOUT = float(os.environ.get("PLAYER_TIMEOUT", "10"))
SAMPLES_FILE = LAB_DIR / "reports" / "samples.json"
RETRY_COUNT = int(os.environ.get("PLAYER_RETRY_COUNT", "3"))
REDACTED_QUERY_KEYS = {"username", "user", "password", "pass", "token", "auth", "api_key", "apikey"}


def make_request(url: str, headers: dict[str, str] | None = None):
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def redact_url(url: str) -> str:
    """Keep reports useful without persisting playlist credentials."""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_query = urllib.parse.urlencode([
        (key, "[REDACTED]" if key.lower() in REDACTED_QUERY_KEYS else value)
        for key, value in query
    ])
    path_parts = parsed.path.split("/")
    for marker in ("movie", "series"):
        try:
            marker_index = path_parts.index(marker)
        except ValueError:
            continue
        if len(path_parts) > marker_index + 2:
            path_parts[marker_index + 1] = "[REDACTED]"
            path_parts[marker_index + 2] = "[REDACTED]"
            break
    else:
        if re.fullmatch(r"/[^/]+/[^/]+/[^/]+\.m3u8", parsed.path, re.I):
            path_parts = ["", "[REDACTED]", "[REDACTED]", path_parts[-1]]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/".join(path_parts), safe_query, ""))


def test_dns_alias(alias_url: str, canonical_host: str = "") -> bool:
    alias_host = urllib.parse.urlsplit(alias_url).hostname or ""
    if not alias_host:
        print("[dns] alias sem hostname")
        return False
    try:
        alias_addresses = {item[4][0] for item in socket.getaddrinfo(alias_host, 443, type=socket.SOCK_STREAM)}
        canonical_addresses = set()
        if canonical_host:
            canonical_addresses = {item[4][0] for item in socket.getaddrinfo(canonical_host, 443, type=socket.SOCK_STREAM)}
        print(f"[dns] {alias_host} -> {sorted(alias_addresses)}")
        if canonical_addresses:
            print(f"[dns] {canonical_host} -> {sorted(canonical_addresses)}")
        return bool(alias_addresses) and (not canonical_addresses or bool(alias_addresses & canonical_addresses))
    except socket.gaierror as exc:
        print(f"[dns] {alias_host} -> ERROR: {exc}")
        return False


def validate_public_playlist(path: Path, canonical_host: str, forbidden_hosts: set[str], allowed_hosts: set[str] | None = None) -> None:
    """Reject origin/alias URLs before any media test can be marked green."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    if not text.startswith("#EXTM3U"):
        raise ValueError("resposta não é uma playlist M3U")
    media_urls = [line.strip() for line in text.splitlines() if line.strip().startswith(("http://", "https://"))]
    if not media_urls:
        raise ValueError("playlist M3U sem URLs de mídia")
    canonical = canonical_host.lower().rstrip(".")
    allowed = {canonical} | {item.lower().rstrip(".") for item in (allowed_hosts or set())}
    for url in media_urls:
        host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
        if host in forbidden_hosts:
            raise ValueError(f"playlist expõe origem/alias proibido: {host}")
        if host not in allowed:
            raise ValueError(f"playlist pública usa host não canônico: {host}")


def test_xtream_handshake(base_url: str, username: str, password: str) -> bool:
    api_url = f"{base_url.rstrip('/')}/player_api.php?username={urllib.parse.quote(username)}&password={urllib.parse.quote(password)}"
    try:
        with make_request(api_url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            user_info = data.get("user_info", {})
            status = str(user_info.get("status", "")).lower()
            exp_date = user_info.get("exp_date")
            print(f"[handshake] {base_url.split('?', 1)[0]} -> status={status} exp_date={exp_date}")
            return status == "active"
    except Exception as exc:  # pragma: no cover - report path
        print(f"[handshake] {redact_url(base_url)} -> ERROR: {redact_url(str(exc))}")
        return False


def test_stream_playback(stream_url: str, label: str, is_vod: bool = False) -> bool:
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            with make_request(stream_url) as resp:
                content_type = resp.headers.get("Content-Type", "")
                code = resp.getcode()
                sample = resp.read(2048)
                print(f"[stream] {label} (try {attempt}) -> HTTP={code} content_type={content_type} bytes={len(sample)}")

            if is_vod:
                with make_request(stream_url, headers={"Range": "bytes=0-1024"}) as resp_range:
                    range_code = resp_range.getcode()
                    content_range = resp_range.headers.get("Content-Range", "")
                    print(f"[range] {label} (try {attempt}) -> HTTP={range_code} content_range={content_range}")
                    return range_code == 206
            return code == 200 and len(sample) > 0
        except urllib.error.URLError as exc:
            print(f"[stream] {label} (try {attempt}) -> ERROR: {redact_url(str(exc))}")
        except Exception as exc:
            print(f"[stream] {label} (try {attempt}) -> ERROR: {redact_url(str(exc))}")
    return False


def extract_sample_urls(m3u_path: Path) -> tuple[str | None, str | None]:
    live_url, vod_url = None, None
    if not m3u_path.exists():
        return None, None
    lines = m3u_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        target_url = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if not target_url.startswith("http"):
            continue
        if "/movie/" in target_url or "/series/" in target_url:
            vod_url = vod_url or target_url
        else:
            live_url = live_url or target_url
        if live_url and vod_url:
            break
    return live_url, vod_url


def classify_playlist_items(m3u_path: Path) -> dict[str, list[dict[str, str]]]:
    items = {"live": [], "movie": [], "series": []}
    if not m3u_path.exists():
        return items

    lines = m3u_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    current_meta = ""
    for index, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            current_meta = line
            continue
        if not line.startswith("http"):
            continue

        name = ""
        match = re.search(r",(.+)$", current_meta)
        if match:
            name = match.group(1).strip()
        group = ""
        match = re.search(r'group-title="([^"]+)"', current_meta, re.I)
        if match:
            group = match.group(1).strip()
        entry = {"url": line, "meta": current_meta, "name": name, "group": group}
        meta = current_meta.lower()
        if "/movie/" in line:
            entry["episode"] = extract_episode_hint(meta)
            items["movie"].append(entry)
        elif "/series/" in line:
            entry["episode"] = extract_episode_hint(meta)
            items["series"].append(entry)
        else:
            items["live"].append(entry)
    return items


def extract_episode_hint(meta: str) -> str:
    match = re.search(r"episode[:=]\s*(\d+)|episodio[:=]\s*(\d+)|s(\d+)e(\d+)", meta, re.I)
    if not match:
        return ""
    if match.group(1):
        return match.group(1)
    if match.group(2):
        return match.group(2)
    if match.group(3):
        return f"s{match.group(3)}e{match.group(4)}"
    return ""


def choose_unique_entries(entries: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in entries:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def rebase_media_url(url: str, base_url: str) -> str:
    """Preserva path/query da amostra e troca somente o endpoint testado."""
    parsed = urllib.parse.urlsplit(url)
    suffix = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
    return f"{base_url.rstrip('/')}{suffix}"


def expected_nginx_location(url: str) -> str:
    """Classifica a location fechada que deveria atender uma amostra."""
    path = urllib.parse.urlsplit(url).path.lower()
    if path.startswith(("/movie/", "/series/")):
        return "vod-relay"
    if re.search(r"/[^/]+/[^/]+/[0-9]+\.m3u8$", path):
        return "broker-manifest"
    return "broker-live"


def write_report(name: str, lines: list[str]) -> None:
    LAB_DIR.joinpath("reports").mkdir(parents=True, exist_ok=True)
    report_path = LAB_DIR / "reports" / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[report] saved -> {report_path}")


def refresh_playlists() -> None:
    if os.environ.get("PLAYER_SKIP_SYNC") == "1":
        return
    sync_script = LAB_DIR / "scripts" / "sync_playlist.sh"
    if not sync_script.is_file():
        raise FileNotFoundError(f"script de sincronização ausente: {sync_script}")
    subprocess.run([str(sync_script)], check=True)


def load_or_build_samples(m3u_path: Path, sample_count: int, refresh_samples: bool = False) -> dict[str, list[dict[str, str]]]:
    if SAMPLES_FILE.exists() and not refresh_samples:
        return json.loads(SAMPLES_FILE.read_text(encoding="utf-8"))

    classified = classify_playlist_items(m3u_path)
    if not any(classified.values()):
        raise ValueError(f"playlist inválida ou resposta HTML sem itens M3U: {m3u_path}")

    def pick_live(entries: list[dict[str, str]]) -> list[dict[str, str]]:
        ufc = [item for item in entries if any(token in item["meta"].lower() for token in ("ufc", "mma")) or "lutas" in item["meta"].lower()]
        lgbt = [item for item in entries if any(token in item["meta"].lower() for token in ("lgbt", "gay", "pride"))]
        chosen = choose_unique_entries(ufc, sample_count) + choose_unique_entries(lgbt, sample_count)
        if len(chosen) < sample_count * 2:
            chosen.extend(choose_unique_entries(entries, sample_count * 2 - len(chosen)))
        return chosen[:sample_count * 2]

    def pick_vod(entries: list[dict[str, str]]) -> list[dict[str, str]]:
        ordered = sorted(entries, key=lambda item: (item.get("name", "").lower(), item["url"]))
        return choose_unique_entries(ordered, sample_count)

    samples = {
        "live": pick_live(classified["live"]),
        "movie": pick_vod(classified["movie"]),
        "series": pick_vod(classified["series"]),
    }
    SAMPLES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAMPLES_FILE.write_text(json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return samples


def main() -> int:
    refresh = "--refresh-samples" in sys.argv
    mode = "both"
    if "--cdn" in sys.argv:
        mode = "cdn"
    elif "--direct" in sys.argv:
        mode = "direct"
    elif "--cname" in sys.argv:
        mode = "cname"
    elif "--both" in sys.argv:
        mode = "both"

    username = os.environ.get("PLAYER_USERNAME", "")
    password = os.environ.get("PLAYER_PASSWORD", "")
    base_cdn = os.environ.get("PLAYER_BASE_CDN", "").rstrip("/")
    base_direct = os.environ.get("PLAYER_BASE_DIRECT", "").rstrip("/")
    base_cname = os.environ.get("PLAYER_BASE_CNAME", "").rstrip("/")
    cname_aliases = [base_cname] + [item.strip().rstrip("/") for item in os.environ.get("PLAYER_BASE_CNAME_ALIASES", "").split(",") if item.strip()]
    cname_aliases = list(dict.fromkeys(item for item in cname_aliases if item))
    latest_default = "cname_latest.m3u8" if mode == "cname" else "cdn_latest.m3u8"
    latest_m3u = Path(os.environ.get("PLAYER_LATEST_PLAYLIST", str(LAB_DIR / "playlists" / latest_default)))
    sample_count = int(os.environ.get("PLAYER_SAMPLE_COUNT", "3"))

    required = {"PLAYER_USERNAME": username, "PLAYER_PASSWORD": password}
    if mode in ("cdn", "both"):
        required["PLAYER_BASE_CDN"] = base_cdn
    if mode in ("direct", "both"):
        required["PLAYER_BASE_DIRECT"] = base_direct
    if mode == "cname":
        required["PLAYER_BASE_CNAME"] = base_cname
        required["PLAYER_BASE_CDN"] = base_cdn
    missing = [key for key, value in required.items() if not value]
    if missing:
        print("Faltam variáveis de ambiente obrigatórias:", ", ".join(missing), file=sys.stderr)
        return 1

    refresh_playlists()
    lines = [f"timestamp={datetime.now().isoformat(timespec='seconds')}"]
    ok = True
    if mode in ("cdn", "both"):
        ok &= test_xtream_handshake(base_cdn, username, password)
    if mode in ("direct", "both"):
        ok &= test_xtream_handshake(base_direct, username, password)
    if mode == "cname":
        canonical_host = urllib.parse.urlsplit(base_cdn).hostname
        allowed_hosts = {canonical_host or ""} | {urllib.parse.urlsplit(item).hostname or "" for item in cname_aliases}
        for alias in cname_aliases:
            ok &= test_dns_alias(alias, canonical_host or "")
            ok &= test_xtream_handshake(alias, username, password)
        try:
            validate_public_playlist(latest_m3u, canonical_host or "", {"38.46.223.77"}, allowed_hosts)
        except (OSError, ValueError) as exc:
            print(f"Falha de segurança na playlist CNAME: {exc}", file=sys.stderr)
            return 2

    try:
        samples = load_or_build_samples(latest_m3u, sample_count, refresh_samples=refresh)
    except (OSError, ValueError) as exc:
        print(f"Falha ao validar playlist: {exc}", file=sys.stderr)
        return 2
    selected_live = samples.get("live", [])
    selected_movie = samples.get("movie", [])
    selected_series = samples.get("series", [])
    lines.append(f"mode={mode}")
    lines.append(f"samples_file={SAMPLES_FILE}")
    lines.append(f"live_selected={len(selected_live)}")
    lines.append(f"movie_selected={len(selected_movie)}")
    lines.append(f"series_selected={len(selected_series)}")

    def playback_suite(base_label: str, base_url: str | None) -> dict[str, list[dict[str, str]]]:
        suite: dict[str, list[dict[str, str]]] = {"live": [], "movie": [], "series": []}
        if not base_url:
            return suite
        suite["live"].extend({"label": f"{base_label}-live-{idx}", "url": rebase_media_url(item["url"], base_url)} for idx, item in enumerate(selected_live[:sample_count], start=1))
        suite["movie"].extend({"label": f"{base_label}-movie-{idx}", "url": rebase_media_url(item["url"], base_url)} for idx, item in enumerate(selected_movie[:sample_count], start=1))
        suite["series"].extend({"label": f"{base_label}-series-{idx}", "url": rebase_media_url(item["url"], base_url)} for idx, item in enumerate(selected_series[:sample_count], start=1))
        return suite

    suites: dict[str, dict[str, list[dict[str, str]]]] = {}
    if mode in ("cdn", "both"):
        suites["cdn"] = playback_suite("cdn", base_cdn)
    if mode in ("direct", "both"):
        suites["direct"] = playback_suite("direct", base_direct)
    if mode == "cname":
        for index, alias in enumerate(cname_aliases, start=1):
            suites[f"cname-{index}"] = playback_suite(f"cname-{index}", alias)

    comparison: list[dict[str, object]] = []
    for route, suite in suites.items():
        for category in ("live", "movie", "series"):
            for item in suite[category]:
                label = item["label"]
                url = item["url"]
                if not url:
                    continue
                result: dict[str, object] = {
                    "route": route, "category": category, "location": expected_nginx_location(url),
                    "label": label, "url": redact_url(url),
                }
                try:
                    status, content_type, body_len = 0, "", 0
                    with make_request(url) as resp:
                        status = resp.getcode()
                        content_type = resp.headers.get("Content-Type", "")
                        body_len = len(resp.read(2048))
                    result.update({"http": status, "content_type": content_type, "bytes": body_len})
                    ok &= status == 200 and body_len > 0
                    if category != "live":
                        with make_request(url, headers={"Range": "bytes=0-1024"}) as resp_range:
                            range_status = resp_range.getcode()
                            content_range = resp_range.headers.get("Content-Range", "")
                            result.update({"range_http": range_status, "content_range": content_range})
                            ok &= range_status == 206
                    else:
                        result.setdefault("range_http", None)
                except Exception as exc:
                    result["error"] = redact_url(str(exc))
                    ok = False
                comparison.append(result)
                print(f"[{route}] {label} -> {result}")

    lines.append(json.dumps(comparison, ensure_ascii=False, indent=2))
    lines.append(f"result={'ok' if ok else 'fail'}")
    write_report("playback", lines)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
