"""Token assinado e compacto para playback adaptativo."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

PLAYBACK_SCOPE = "playback"
DEFAULT_SECRET = "cdnmnus-playback-dev-secret"


def _secret() -> bytes:
    return os.environ.get("CDNMNUS_PLAYBACK_TOKEN_SECRET", DEFAULT_SECRET).encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def build_claims(*, session_id: str, tenant_id: str, channel_id: str, edge_id: str,
                 expires_at: int | None = None, jti: str | None = None) -> dict[str, Any]:
    now = int(time.time())
    return {
        "sid": session_id,
        "tid": tenant_id,
        "cid": channel_id,
        "eid": edge_id,
        "scope": PLAYBACK_SCOPE,
        "exp": int(expires_at or (now + 300)),
        "jti": jti or uuid.uuid4().hex,
    }


def sign_claims(claims: dict[str, Any], *, secret: bytes | None = None) -> str:
    payload = json.dumps(claims, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    key = secret or _secret()
    digest = hmac.new(key, payload, hashlib.sha256).digest()
    return f"pb1.{_b64(payload)}.{_b64(digest)}"


def verify_token(token: str, *, secret: bytes | None = None) -> dict[str, Any]:
    if not token.startswith("pb1."):
        raise ValueError("token de playback inválido")
    try:
        payload_b64, sig_b64 = token[4:].rsplit(".", 1)
        payload = _unb64(payload_b64)
        signature = _unb64(sig_b64)
    except ValueError as exc:
        raise ValueError("token de playback inválido") from exc
    key = secret or _secret()
    expected = hmac.new(key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise PermissionError("assinatura de playback inválida")
    claims = json.loads(payload.decode("utf-8"))
    if claims.get("scope") != PLAYBACK_SCOPE:
        raise PermissionError("escopo de playback inválido")
    if int(claims.get("exp", 0)) < int(time.time()):
        raise PermissionError("token de playback expirado")
    return claims


def build_playback_url(edge_host: str, session_id: str, token: str, *, media_type: str,
                       channel_id: str) -> str:
    from urllib.parse import urlencode, urlunsplit

    path = f"/playback/{media_type}/{session_id}"
    query = urlencode({"token": token, "channel_id": channel_id})
    return urlunsplit(("https", edge_host, path, query, ""))
