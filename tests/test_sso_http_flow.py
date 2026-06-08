# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

from __future__ import annotations

import json
from dataclasses import dataclass

from lib.codex_oauth import OAuthStart
from lib.idp_client import GeneratedAccount
from lib.sso_http_flow import SSOHttpFlow, parse_html_forms, populate_account_form


@dataclass
class FakeResponse:
    status_code: int
    url: str
    headers: dict
    text: str = ""
    payload: dict | None = None

    def json(self):
        if self.payload is None:
            raise ValueError("no json")
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url == "https://idp.example/start":
            return FakeResponse(200, url, {}, '<form action="/login" method="post"><input name="email"><input name="password" type="password"><button name="go" value="1"></button></form>')
        if url == "https://idp.example/login":
            assert kwargs["data"]["email"] == "u@example.com"
            assert kwargs["data"]["password"] == "Pw123!"
            return FakeResponse(302, url, {"Location": "https://idp.example/done"})
        if url == "https://idp.example/done":
            return FakeResponse(200, url, {}, "ok")
        if url.startswith("https://auth.openai.com/oauth/authorize"):
            return FakeResponse(302, url, {"Location": "http://localhost:1455/auth/callback?code=code_1&state=state_1"})
        if url == "https://auth.openai.com/oauth/token":
            return FakeResponse(200, url, {}, json.dumps({"access_token": "acc", "refresh_token": "ref", "id_token": "", "expires_in": 3600}), {"access_token": "acc", "refresh_token": "ref", "id_token": "", "expires_in": 3600})
        raise AssertionError(url)


def test_parse_and_populate_form():
    forms, links, meta = parse_html_forms('<form action="/x" method="post"><input name="username"><input name="passwd" type="password"></form>')
    assert len(forms) == 1
    account = GeneratedAccount(id=1, email="u@example.com", password="Pw123!")
    data = populate_account_form(forms[0], account)
    assert data["username"] == "u@example.com"
    assert data["passwd"] == "Pw123!"


def test_sso_http_flow_mock_end_to_end(tmp_path):
    session = FakeSession()
    flow = SSOHttpFlow(session=session, artifact_dir=tmp_path)
    account = GeneratedAccount(id=1, email="u@example.com", password="Pw123!")
    oauth = OAuthStart(
        auth_url="https://auth.openai.com/oauth/authorize?state=state_1",
        state="state_1",
        code_verifier="verifier",
        redirect_uri="http://localhost:1455/auth/callback",
        client_id="client_1",
        scope="openid",
    )
    token = flow.run(start_url="https://idp.example/start", oauth=oauth, account=account)
    assert token["refresh_token"] == "ref"
    assert any(call[1] == "https://auth.openai.com/oauth/token" for call in session.calls)
