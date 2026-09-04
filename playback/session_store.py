"""Persistência local de sessões de playback com janela curta e auditoria."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from playback.route_policy import (
    EDGE_COOLDOWN_SECONDS,
    GLOBAL_COOLDOWN_SECONDS,
    build_cooldown_key,
    normalize_channel_id,
    normalize_media_type,
    pick_initial_edge,
    should_switch,
)
from playback.token import build_claims, build_playback_url, sign_claims, verify_token


def normalize_media_uri(value: str) -> str:
    """Keep only relative media paths; credentials and absolute URLs are rejected."""
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if not raw or len(raw) > 4096 or parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("media_uri inválida")
    if ".." in parsed.path.split("/") or not parsed.path.startswith(("/hls/", "/live/", "/movie/", "/series/", "/play/")):
        raise ValueError("media_uri inválida")
    if any(key.split("=", 1)[0].lower() in {"user", "username", "pass", "password", "token", "auth"}
           for key in parsed.query.split("&") if key):
        raise ValueError("media_uri não pode conter credenciais")
    if re.match(r"^/(?:hls|live|movie|series)/[^/]+/[^/]+/", parsed.path, re.IGNORECASE):
        raise ValueError("media_uri credenciada; use o token opaco da playlist")
    return parsed.path + (("?" + parsed.query) if parsed.query else "")


class SessionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def initialize(self) -> None:
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS playback_sessions (
                    session_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    media_type TEXT NOT NULL CHECK(media_type IN ('live','vod')),
                    media_uri TEXT NOT NULL DEFAULT '',
                    edge_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','degraded','switched','expired')),
                    switch_count INTEGER NOT NULL DEFAULT 0,
                    attempted_edges_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_event_at REAL,
                    last_sequence INTEGER,
                    last_event_id TEXT,
                    last_reason TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS playback_sessions_tenant_channel
                    ON playback_sessions(tenant_id, channel_id, edge_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS playback_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES playback_sessions(session_id) ON DELETE CASCADE,
                    tenant_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    edge_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    sequence INTEGER,
                    observed_at REAL NOT NULL,
                    accepted INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS playback_events_session_time
                    ON playback_events(session_id, observed_at, created_at);
                CREATE TABLE IF NOT EXISTS playback_edge_cooldowns (
                    cooldown_key TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    edge_id TEXT NOT NULL,
                    until_at REAL NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS playback_cooldowns_tenant_edge
                    ON playback_edge_cooldowns(tenant_id, channel_id, edge_id, until_at);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(playback_sessions)")}
            if "media_uri" not in columns:
                db.execute("ALTER TABLE playback_sessions ADD COLUMN media_uri TEXT NOT NULL DEFAULT ''")
        try:
            self.path.chmod(0o600)
        except PermissionError:
            pass

    def _now(self, now: float | None = None) -> float:
        return float(time.time() if now is None else now)

    def _session_row(self, db: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM playback_sessions WHERE session_id=?", (session_id,)).fetchone()

    def _cooldown_rows(self, db: sqlite3.Connection, tenant_id: str, channel_id: str) -> list[sqlite3.Row]:
        return db.execute(
            """SELECT * FROM playback_edge_cooldowns
               WHERE tenant_id=? AND channel_id=? AND until_at > ?
               ORDER BY until_at DESC, edge_id""",
            (tenant_id, channel_id, self._now()),
        ).fetchall()

    def _edge_maps(self, edges: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in edges:
            host = str(item.get("host") or item.get("id") or "").strip().lower()
            if not host or host in seen:
                continue
            seen.add(host)
            result.append(dict(item, host=host, id=str(item.get("id") or host)))
        return result

    def resolve_playback(self, session_id: str, token: str, *, tenant_id: str,
                         channel_id: str, media_type: str, secret: bytes | None = None) -> dict[str, Any]:
        """Validate the session binding before Nginx performs an internal redirect."""
        claims = verify_token(token, secret=secret)
        channel_id = normalize_channel_id(channel_id)
        media_type = normalize_media_type(media_type)
        if claims.get("sid") != session_id or claims.get("tid") != tenant_id:
            raise PermissionError("token de playback não pertence ao tenant/sessão")
        if claims.get("cid") != channel_id:
            raise PermissionError("canal do token divergente")
        with closing(self.connect()) as db:
            row = self._session_row(db, session_id)
            if row is None or row["tenant_id"] != tenant_id or row["channel_id"] != channel_id:
                raise PermissionError("sessão de playback inválida")
            if row["media_type"] != media_type or row["state"] == "expired":
                raise PermissionError("tipo ou estado de playback inválido")
            if row["edge_id"] != claims.get("eid"):
                raise PermissionError("edge do token não é a edge atual")
            if not row["media_uri"]:
                raise LookupError("sessão sem media_uri")
            return {"edge_id": row["edge_id"], "media_uri": row["media_uri"], "session_id": session_id}

    def create_session(self, tenant_id: str, channel_id: str, media_type: str,
                       edges: Iterable[Mapping[str, Any]], *, ttl_seconds: int = 900,
                       now: float | None = None, secret: bytes | None = None,
                       media_uri: str = "", public_host: str | None = None) -> dict[str, Any]:
        channel_id = normalize_channel_id(channel_id)
        media_type = normalize_media_type(media_type)
        media_uri = normalize_media_uri(media_uri) if media_uri else ""
        ts = self._now(now)
        candidates = self._edge_maps(edges)
        with self.connect() as db:
            cooldowns = {row["edge_id"] for row in self._cooldown_rows(db, tenant_id, channel_id)}
            edge = pick_initial_edge(candidates, excluded=cooldowns, now=ts)
            if edge is None:
                raise LookupError("nenhuma edge elegível")
            session_id = "ps-" + uuid.uuid4().hex
            expires_at = ts + max(60, min(3600, int(ttl_seconds)))
            claims = build_claims(
                session_id=session_id,
                tenant_id=tenant_id,
                channel_id=channel_id,
                edge_id=str(edge["id"]),
                expires_at=int(expires_at),
            )
            token = sign_claims(claims, secret=secret)
            play_url = build_playback_url(public_host or str(edge["host"]), session_id, token, media_type=media_type, channel_id=channel_id)
            db.execute(
                """INSERT INTO playback_sessions(
                       session_id,tenant_id,channel_id,media_type,media_uri,edge_id,state,
                       switch_count,attempted_edges_json,created_at,expires_at,
                       last_event_at,last_sequence,last_event_id,last_reason,updated_at
                   ) VALUES(?,?,?,?,?,?,'active',0,?,?,?,?,NULL,NULL,NULL,?)""",
                (session_id, tenant_id, channel_id, media_type, media_uri, str(edge["id"]),
                 json.dumps([str(edge["id"])], sort_keys=True), ts, expires_at, ts, ts),
            )
        return {
            "session_id": session_id,
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "media_type": media_type,
            "media_uri": media_uri,
            "edge_id": str(edge["id"]),
            "play_url": play_url,
            "telemetry_url": f"/api/playback/sessions/{session_id}/events",
            "expires_in": int(expires_at - ts),
        }

    def record_event(self, session_id: str, payload: Mapping[str, Any], edges: Iterable[Mapping[str, Any]],
                     *, now: float | None = None, secret: bytes | None = None,
                     public_host: str | None = None) -> dict[str, Any]:
        ts = self._now(now)
        event_id = str(payload.get("event_id") or "").strip()
        tenant_id = str(payload.get("tenant_id") or "").strip()
        channel_id = normalize_channel_id(str(payload.get("channel_id") or ""))
        edge_id = str(payload.get("edge_id") or "").strip()
        event_type = str(payload.get("type") or "").strip().lower()
        allowed_event_types = {
            "playlist_timeout", "segment_timeout", "connection_refused", "http_502", "http_503", "http_504",
            "edge_authorization_failed", "vod_range_failed", "heartbeat_lost", "rebuffer", "playback_started",
            "playback_stopped", "heartbeat",
        }
        if event_type not in allowed_event_types:
            raise ValueError("tipo de evento de playback inválido")
        sequence = payload.get("sequence")
        observed_at = payload.get("observed_at", ts)
        try:
            observed_at_value = float(observed_at)
        except (TypeError, ValueError):
            observed_at_value = ts
        candidates = self._edge_maps(edges)
        with self.connect() as db:
            session = self._session_row(db, session_id)
            if session is None:
                raise LookupError("sessão inexistente")
            if session["tenant_id"] != tenant_id:
                raise PermissionError("tenant da sessão divergente")
            if session["channel_id"] != channel_id:
                raise PermissionError("canal da sessão divergente")
            if session["edge_id"] != edge_id:
                raise PermissionError("edge da sessão divergente")
            if float(session["expires_at"]) < ts:
                db.execute("UPDATE playback_sessions SET state='expired', updated_at=? WHERE session_id=?", (ts, session_id))
                raise PermissionError("sessão expirada")
            if event_id and db.execute("SELECT 1 FROM playback_events WHERE event_id=?", (event_id,)).fetchone():
                raise ValueError("event_id duplicado")
            last_sequence = session["last_sequence"]
            if sequence is not None and last_sequence is not None and int(sequence) <= int(last_sequence):
                action = "ignored"
                reason = "sequência repetida"
            else:
                action = "keep_current"
                reason = ""
                history = db.execute(
                    "SELECT * FROM playback_events WHERE session_id=? AND observed_at >= ? ORDER BY observed_at, created_at",
                    (session_id, ts - 60),
                ).fetchall()
                trigger_payloads = [
                    {"type": row["event_type"], "observed_at": row["observed_at"]}
                    for row in history
                ]
                trigger_payloads.append({"type": event_type, "observed_at": observed_at_value})
                event_action = "keep_current"
                if should_switch(session, trigger_payloads, now=ts):
                    current = str(session["edge_id"])
                    cooldowns = {row["edge_id"] for row in self._cooldown_rows(db, tenant_id, channel_id)}
                    excluded = {current, *cooldowns, *json.loads(session["attempted_edges_json"] or "[]")}
                    edge = pick_initial_edge(candidates, excluded=excluded, now=ts)
                    if edge is not None and int(session["switch_count"] or 0) < 3:
                        next_edge = str(edge["id"])
                        if next_edge != current:
                            expires_at = ts + max(60, int(session["expires_at"] - float(session["created_at"])))
                            claims = build_claims(
                                session_id=session_id,
                                tenant_id=tenant_id,
                                channel_id=channel_id,
                                edge_id=next_edge,
                                expires_at=int(expires_at),
                            )
                            token = sign_claims(claims, secret=secret)
                            play_url = build_playback_url(
                                public_host or str(edge["host"]),
                                session_id,
                                token,
                                media_type=str(session["media_type"]),
                                channel_id=channel_id,
                            )
                            attempted = list(json.loads(session["attempted_edges_json"] or "[]"))
                            if next_edge not in attempted:
                                attempted.append(next_edge)
                            db.execute(
                                """UPDATE playback_sessions
                                   SET edge_id=?, state='switched', switch_count=switch_count+1,
                                       attempted_edges_json=?, last_event_at=?, last_sequence=?,
                                       last_event_id=?, last_reason=?, updated_at=?
                                   WHERE session_id=?""",
                                (next_edge, json.dumps(attempted, sort_keys=True), observed_at_value,
                                 None if sequence is None else int(sequence), event_id or None,
                                 "segment_timeout_threshold", ts, session_id),
                            )
                            cooldown_key = build_cooldown_key(tenant_id, channel_id, current)
                            db.execute(
                                """INSERT OR REPLACE INTO playback_edge_cooldowns(
                                       cooldown_key,tenant_id,channel_id,edge_id,until_at,reason,created_at
                                   ) VALUES(?,?,?,?,?,?,?)""",
                                (cooldown_key, tenant_id, channel_id, current, ts + EDGE_COOLDOWN_SECONDS,
                                 "switch_edge", ts),
                            )
                            global_key = build_cooldown_key(tenant_id, channel_id, next_edge + ":global")
                            db.execute(
                                """INSERT OR REPLACE INTO playback_edge_cooldowns(
                                       cooldown_key,tenant_id,channel_id,edge_id,until_at,reason,created_at
                                   ) VALUES(?,?,?,?,?,?,?)""",
                                (global_key, tenant_id, channel_id, next_edge, ts + GLOBAL_COOLDOWN_SECONDS,
                                 "global_cooldown", ts),
                            )
                            event_action = "switch_edge"
                            reason = "segment_timeout_threshold"
                            edge_id = next_edge
                            session = db.execute("SELECT * FROM playback_sessions WHERE session_id=?", (session_id,)).fetchone()
                            play_value = play_url
                        else:
                            play_value = None
                    else:
                        play_value = None
                else:
                    play_value = None
                action = event_action
                if action == "keep_current":
                    db.execute(
                        """UPDATE playback_sessions
                           SET state='degraded', last_event_at=?, last_sequence=?,
                               last_event_id=?, last_reason=?, updated_at=?
                           WHERE session_id=?""",
                        (
                            observed_at_value,
                            None if sequence is None else int(sequence),
                            event_id or None,
                            event_type,
                            ts,
                            session_id,
                        ),
                    )
            db.execute(
                """INSERT INTO playback_events(
                       event_id,session_id,tenant_id,channel_id,edge_id,event_type,
                       sequence,observed_at,accepted,action,reason,payload_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id or "evt-" + uuid.uuid4().hex,
                    session_id,
                    tenant_id,
                    channel_id,
                    edge_id,
                    event_type,
                    None if sequence is None else int(sequence),
                    observed_at_value,
                    0 if action == "ignored" else 1,
                    action,
                    reason,
                    json.dumps({k: v for k, v in payload.items() if k not in {"token", "password"}}, sort_keys=True, ensure_ascii=False),
                    ts,
                ),
            )
            if action == "switch_edge":
                return {
                    "action": action,
                    "session_id": session_id,
                    "edge_id": edge_id,
                    "play_url": play_value,
                    "reason": reason,
                    "expires_in": int(max(60, float(session["expires_at"]) - ts)),
                }
            return {
                "action": "keep_current",
                "session_id": session_id,
                "retry_after": 10,
            }
