"""Política pura de seleção e troca de edge para playback adaptativo."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

WINDOW_SECONDS = 60
THRESHOLD_ERRORS = 3
MAX_SWITCHES = 3
EDGE_COOLDOWN_SECONDS = 60
GLOBAL_COOLDOWN_SECONDS = 120
TRIGGERING_ERRORS = {
    "playlist_timeout",
    "segment_timeout",
    "connection_refused",
    "http_502",
    "http_503",
    "http_504",
    "edge_authorization_failed",
    "vod_range_failed",
    "heartbeat_lost",
}


def _now_ts(now: float | None = None) -> float:
    return float(datetime.now(timezone.utc).timestamp() if now is None else now)


def normalize_media_type(media_type: str) -> str:
    media_type = media_type.strip().lower()
    if media_type not in {"live", "vod"}:
        raise ValueError("media_type inválido")
    return media_type


def normalize_channel_id(channel_id: str) -> str:
    channel_id = channel_id.strip()
    if not channel_id or len(channel_id) > 128 or any(ord(ch) < 32 for ch in channel_id):
        raise ValueError("channel_id inválido")
    return channel_id


def normalize_edge(edge: Mapping[str, Any]) -> dict[str, Any]:
    host = str(edge.get("host") or edge.get("edge_id") or edge.get("id") or "").strip().lower()
    if not host:
        raise ValueError("edge sem host")
    state = str(edge.get("state", "ready")).strip().lower()
    return {
        "id": str(edge.get("id") or host),
        "host": host,
        "state": state,
        "weight": int(edge.get("weight", 100)),
        "last_health_status": edge.get("last_health_status"),
        "last_health_at": edge.get("last_health_at"),
        "pressure": float(edge.get("pressure", edge.get("capacity_pressure", 0.0)) or 0.0),
    }


def eligible_edges(edges: Iterable[Mapping[str, Any]], *, excluded: Iterable[str] = (),
                   now: float | None = None) -> list[dict[str, Any]]:
    ts = _now_ts(now)
    excluded_set = {str(item).lower() for item in excluded}
    candidates: list[dict[str, Any]] = []
    for raw in edges:
        edge = normalize_edge(raw)
        if edge["host"] in excluded_set or edge["id"].lower() in excluded_set:
            continue
        if edge["state"] not in {"ready", "pressured"}:
            continue
        health = edge.get("last_health_status")
        if health not in (None, 200):
            continue
        last_health_at = edge.get("last_health_at")
        if last_health_at:
            try:
                age = ts - datetime.fromisoformat(str(last_health_at).replace("Z", "+00:00")).timestamp()
            except ValueError:
                age = WINDOW_SECONDS + 1
            if age > 300:
                continue
        candidates.append(edge)
    candidates.sort(key=lambda item: (
        0 if item["state"] == "ready" else 1,
        round(item["pressure"], 4),
        -int(item.get("weight", 100)),
        item["host"],
    ))
    return candidates


def pick_initial_edge(edges: Iterable[Mapping[str, Any]], *, excluded: Iterable[str] = (),
                      now: float | None = None) -> dict[str, Any] | None:
    candidates = eligible_edges(edges, excluded=excluded, now=now)
    return candidates[0] if candidates else None


def recent_trigger_count(events: Iterable[Mapping[str, Any]], *, now: float | None = None,
                         window_seconds: int = WINDOW_SECONDS) -> int:
    ts = _now_ts(now)
    count = 0
    for event in events:
        if str(event.get("type", "")).strip().lower() not in TRIGGERING_ERRORS:
            continue
        observed_at = event.get("observed_at")
        if observed_at is None:
            continue
        try:
            event_ts = float(observed_at)
        except (TypeError, ValueError):
            try:
                event_ts = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        if ts - event_ts <= window_seconds:
            count += 1
    return count


def should_switch(session: Mapping[str, Any], events: Iterable[Mapping[str, Any]], *,
                  now: float | None = None) -> bool:
    try:
        switch_count = session["switch_count"]
    except Exception:
        switch_count = session.get("switch_count", 0)
    try:
        state = session["state"]
    except Exception:
        state = session.get("state", "active")
    if int(switch_count or 0) >= MAX_SWITCHES:
        return False
    if str(state or "active") == "expired":
        return False
    return recent_trigger_count(events, now=now) >= THRESHOLD_ERRORS


def build_cooldown_key(tenant_id: str, channel_id: str, edge_id: str) -> str:
    return f"{tenant_id}:{channel_id}:{edge_id}"
