# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

from __future__ import annotations

from lib.idp_client import GeneratedAccount, IdpClient


def test_idp_client_calls_expected_endpoints():
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/api/user/bootstrap"):
            return {"channels": [{"id": 1}], "domains_by_channel": {}}
        if path == "/api/user/me":
            return {"customer": {"name": "n", "points": 10}, "accounts": {"items": []}}
        if path == "/api/user/generate":
            return {"account": {"id": 123, "email": "u@example.com", "password": "Pw123!", "name": "U"}}
        if path == "/api/user/start-sso":
            return {"start_url": "https://example.com/sso"}
        raise AssertionError(path)

    client = IdpClient(request_json=fake_request)
    assert client.bootstrap(client_id="openai-client1")["channels"]
    assert client.me(token="tok")["customer"]["points"] == 10
    account = client.generate_account(token="tok", channel_id="1")
    assert isinstance(account, GeneratedAccount)
    assert account.email == "u@example.com"
    assert account.password == "Pw123!"
    assert client.start_sso(token="tok", account_id=account.id) == "https://example.com/sso"

    assert calls[0][0] == "GET"
    assert calls[0][1] == "/api/user/bootstrap?client_id=openai-client1"
    assert calls[2][2]["channel_id"] == 1
