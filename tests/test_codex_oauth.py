# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

from __future__ import annotations

import base64
import json

from lib.codex_oauth import generate_oauth_start, parse_callback_url, token_config_from_response


def _jwt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"header.{body}.sig"


def test_pkce_callback_and_token_config():
    start = generate_oauth_start(redirect_uri="http://localhost:1455/auth/callback")
    assert "code_challenge_method=S256" in start.auth_url
    assert len(start.code_verifier) >= 43

    parsed = parse_callback_url("http://localhost:1455/auth/callback?code=abc&state=" + start.state, expected_state=start.state)
    assert parsed["code"] == "abc"
    assert parsed["state"] == start.state

    token = token_config_from_response(
        {
            "access_token": "acc",
            "refresh_token": "ref",
            "id_token": _jwt({"email": "user@example.com", "https://api.openai.com/auth": {"chatgpt_account_id": "acct_1", "user_id": "user_1"}}),
            "expires_in": 3600,
        },
        client_id="client_1",
    )
    assert token["email"] == "user@example.com"
    assert token["account_id"] == "acct_1"
    assert token["user_id"] == "user_1"
    assert token["type"] == "codex"
