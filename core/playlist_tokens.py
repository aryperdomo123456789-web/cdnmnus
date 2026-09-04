"""Armazenamento local de tokens opacos para URLs de mídia tokenizadas."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

DEFAULT_STORE = "/run/cdnmnus/playlist-tokens.db"


class PlaylistTokenStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_STORE)
        self.redis_url = os.environ.get("CDNMNUS_REDIS_URL", "").strip()
        self._redis_client = None

    def _redis(self):
        if not self.redis_url:
            return None
        if self._redis_client is None:
            try:
                import redis  # type: ignore[import-not-found]
                client = redis.Redis.from_url(self.redis_url, decode_responses=True,
                                              socket_timeout=3, socket_connect_timeout=3)
                client.ping()
                self._redis_client = client
            except Exception as exc:
                raise RuntimeError("Redis configurado, mas indisponível") from exc
        return self._redis_client

    @staticmethod
    def _redis_key(token_hash: str) -> str:
        return f"cdnmnus:playlist-token:{token_hash}"

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def initialize(self) -> None:
        if self._redis() is not None:
            return
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS playlist_tokens (
                token_hash TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                upstream_host TEXT NOT NULL,
                internal_uri TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS playlist_tokens_expiry ON playlist_tokens(expires_at)")
        try:
            self.path.chmod(0o600)
        except PermissionError:
            pass

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def issue(self, tenant_id: str, upstream_host: str, internal_uri: str,
              *, ttl_seconds: int = 300, now: float | None = None,
              connection: sqlite3.Connection | None = None) -> str:
        if not tenant_id or not upstream_host or not internal_uri.startswith("/"):
            raise ValueError("mapeamento de playlist inválido")
        if ".." in internal_uri.split("/", 1)[-1].split("?")[0].split("/"):
            raise ValueError("caminho de playlist inválido")
        ts = float(time.time() if now is None else now)
        expires_at = ts + max(30, min(3600, int(ttl_seconds)))
        token = "pt1_" + secrets.token_urlsafe(32)
        redis_client = self._redis()
        if redis_client is not None:
            redis_client.setex(self._redis_key(self._hash(token)), int(expires_at - ts), json.dumps({
                "tenant_id": tenant_id, "upstream_host": upstream_host.lower(),
                "internal_uri": internal_uri, "expires_at": expires_at,
            }, separators=(",", ":")))
            return token
        if connection is None:
            with self.connect() as db:
                db.execute("INSERT INTO playlist_tokens VALUES(?,?,?,?,?,?)",
                           (self._hash(token), tenant_id, upstream_host.lower(), internal_uri, expires_at, ts))
        else:
            connection.execute("INSERT INTO playlist_tokens VALUES(?,?,?,?,?,?)",
                               (self._hash(token), tenant_id, upstream_host.lower(), internal_uri, expires_at, ts))
        return token

    def resolve(self, token: str, tenant_id: str, *, now: float | None = None) -> dict[str, Any]:
        if not token.startswith("pt1_") or len(token) > 128:
            raise PermissionError("token de mídia inválido")
        ts = float(time.time() if now is None else now)
        redis_client = self._redis()
        if redis_client is not None:
            raw = redis_client.get(self._redis_key(self._hash(token)))
            if not raw:
                raise PermissionError("token de mídia inválido")
            try:
                item = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise PermissionError("token de mídia inválido") from exc
            if item.get("tenant_id") != tenant_id or float(item.get("expires_at", 0)) < ts:
                raise PermissionError("token de mídia inválido")
            return item
        with self.connect() as db:
            row = db.execute("SELECT * FROM playlist_tokens WHERE token_hash=?", (self._hash(token),)).fetchone()
            if row is None or row[1] != tenant_id or float(row[4]) < ts:
                raise PermissionError("token de mídia inválido")
            db.execute("DELETE FROM playlist_tokens WHERE expires_at < ?", (ts,))
            return {"tenant_id": row[1], "upstream_host": row[2], "internal_uri": row[3], "expires_at": row[4]}
