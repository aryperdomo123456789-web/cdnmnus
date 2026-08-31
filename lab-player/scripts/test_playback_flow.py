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

LAB_DIR = Path(os.environ.get("LAB_DIR", "/opt/cdnmnus/lab-player"))
USER_AGENT = os.environ.get("PLAYER_USER_AGENT", "IBOPlayerPro/3.6.0 (Android TV; ExoPlayerLib/2.18.7)")
TIMEOUT = float(os.environ.get("PLAYER_TIMEOUT", "10"))
SAMPLES_FILE = LAB_DIR / "reports" / "samples.json"
RETRY_COUNT = int(os.environ.get("PLAYER_RETRY_COUNT", "3"))


def make_request(url: str, headers: dict[str, str] | None = None):
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def test_xtream_handshake(base_url: str, username: str, password: str) -> bool:
    api_url = f"{base_url.rstrip('/')}/player_api.php?username={urllib.parse.quote(username)}&password={urllib.parse.quote(password)}"
    try:
        with make_request(api_url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            user_info = data.get("user_info", {})
            status = str(user_info.get("status", "")).lower()
            exp_date = user_info.get("exp_date")
            print(f"[handshake] {base_url} -> status={status} exp_date={exp_date}")
            return status == "active"
    except Exception as exc:  # pragma: no cover - report path
        print(f"[handshake] {base_url} -> ERROR: {exc}")
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
            print(f"[stream] {label} (try {attempt}) -> ERROR: {exc}")
        except Exception as exc:
            print(f"[stream] {label} (try {attempt}) -> ERROR: {exc}")
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


def write_report(name: str, lines: list[str]) -> None:
    LAB_DIR.joinpath("reports").mkdir(parents=True, exist_ok=True)
    report_path = LAB_DIR / "reports" / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[report] saved -> {report_path}")


def refresh_playlists() -> None:
    sync_script = LAB_DIR / "scripts" / "sync_playlist.sh"
    if not sync_script.is_file():
        raise FileNotFoundError(f"script de sincronização ausente: {sync_script}")
    subprocess.run([str(sync_script)], check=True)


def load_or_build_samples(m3u_path: Path, sample_count: int, refresh_samples: bool = False) -> dict[str, list[dict[str, str]]]:
    if SAMPLES_FILE.exists() and not refresh_samples:
        return json.loads(SAMPLES_FILE.read_text(encoding="utf-8"))

    classified = classify_playlist_items(m3u_path)

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
    elif "--both" in sys.argv:
        mode = "both"

    username = os.environ.get("PLAYER_USERNAME", "")
    password = os.environ.get("PLAYER_PASSWORD", "")
    base_cdn = os.environ.get("PLAYER_BASE_CDN", "").rstrip("/")
    base_direct = os.environ.get("PLAYER_BASE_DIRECT", "").rstrip("/")
    latest_m3u = Path(os.environ.get("PLAYER_LATEST_PLAYLIST", str(LAB_DIR / "playlists/cdn_latest.m3u8")))
    sample_count = int(os.environ.get("PLAYER_SAMPLE_COUNT", "3"))

    missing = [key for key, value in {
        "PLAYER_USERNAME": username,
        "PLAYER_PASSWORD": password,
        "PLAYER_BASE_CDN": base_cdn,
        "PLAYER_BASE_DIRECT": base_direct,
    }.items() if not value]
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

    samples = load_or_build_samples(latest_m3u, sample_count, refresh_samples=refresh)
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

    comparison: list[dict[str, object]] = []
    for route, suite in suites.items():
        for category in ("live", "movie", "series"):
            for item in suite[category]:
                label = item["label"]
                url = item["url"]
                if not url:
                    continue
                result: dict[str, object] = {"route": route, "category": category, "label": label, "url": url}
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
                    result["error"] = str(exc)
                    ok = False
                comparison.append(result)
                print(f"[{route}] {label} -> {result}")

    lines.append(json.dumps(comparison, ensure_ascii=False, indent=2))
    lines.append(f"result={'ok' if ok else 'fail'}")
    write_report("playback", lines)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
