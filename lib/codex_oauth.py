# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

"""Independent Codex OAuth/PKCE helpers."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_SCOPE = "openid profile email offline_access"


@dataclass(frozen=True)
class OAuthStart:
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str
    client_id: str = DEFAULT_CLIENT_ID
    scope: str = DEFAULT_SCOPE


def b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def sha256_b64url_no_pad(value: str) -> str:
    return b64url_no_pad(hashlib.sha256(value.encode("ascii")).digest())


def generate_oauth_start(
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    client_id: str = DEFAULT_CLIENT_ID,
    scope: str = DEFAULT_SCOPE,
) -> OAuthStart:
    state = secrets.token_urlsafe(16)
    code_verifier = secrets.token_urlsafe(64)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": sha256_b64url_no_pad(code_verifier),
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return OAuthStart(
        auth_url=f"{AUTH_URL}?{urllib.parse.urlencode(params)}",
        state=state,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
        scope=scope,
    )


def parse_callback_url(callback_url: str, *, expected_state: str = "") -> dict[str, str]:
    candidate = str(callback_url or "").strip()
    if not candidate:
        return {"code": "", "state": "", "error": "", "error_description": ""}
    if "://" not in candidate:
        if candidate.startswith("?"):
            candidate = f"http://localhost/{candidate}"
        elif "=" in candidate and not any(ch in candidate for ch in "/?#"):
            candidate = f"http://localhost/?{candidate}"
        else:
            candidate = f"http://{candidate}"
    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)
    for key, values in fragment.items():
        if key not in query or not (query[key][0] if query[key] else ""):
            query[key] = values

    def get1(key: str) -> str:
        values = query.get(key, [""])
        return str(values[0] or "").strip()

    out = {
        "code": get1("code"),
        "state": get1("state"),
        "error": get1("error"),
        "error_description": get1("error_description"),
    }
    if out["error"]:
        raise RuntimeError(f"oauth error: {out['error']}: {out['error_description']}")
    if expected_state and out["state"] != expected_state:
        raise ValueError("state mismatch")
    return out


def jwt_claims_no_verify(token: str) -> dict[str, Any]:
    raw = str(token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    padding = "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode((payload_b64 + padding).encode("ascii")).decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def token_config_from_response(token_resp: dict[str, Any], *, client_id: str = DEFAULT_CLIENT_ID) -> dict[str, Any]:
    access_token = str(token_resp.get("access_token") or "").strip()
    refresh_token = str(token_resp.get("refresh_token") or "").strip()
    id_token = str(token_resp.get("id_token") or "").strip()
    claims = jwt_claims_no_verify(id_token)
    auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    profile_claims = claims.get("https://api.openai.com/profile") if isinstance(claims.get("https://api.openai.com/profile"), dict) else {}
    email = str(claims.get("email") or profile_claims.get("email") or "").strip()
    account_id = str(auth_claims.get("chatgpt_account_id") or auth_claims.get("account_id") or "").strip()
    user_id = str(auth_claims.get("user_id") or auth_claims.get("chatgpt_user_id") or claims.get("sub") or "").strip()
    try:
        expires_in = int(token_resp.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    now = int(time.time())
    return {
        "id_token": id_token,
        "client_id": client_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "user_id": user_id,
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "email": email,
        "type": "codex",
        "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 0))),
        "claims": claims,
    }


def public_token_result(token_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": token_config.get("email") or "",
        "account_id": token_config.get("account_id") or "",
        "user_id": token_config.get("user_id") or "",
        "expired": token_config.get("expired") or "",
        "has_access_token": bool(str(token_config.get("access_token") or "").strip()),
        "has_refresh_token": bool(str(token_config.get("refresh_token") or "").strip()),
        "has_id_token": bool(str(token_config.get("id_token") or "").strip()),
    }
