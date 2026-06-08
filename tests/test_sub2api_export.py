# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

from __future__ import annotations

from lib.sub2api_export import OAuthExportRecord, Sub2ApiConfig, Sub2ApiExportProvider


def test_sub2api_payload_and_push():
    requests = []

    def fake_request(method, url, **kwargs):
        requests.append((method, url, kwargs))
        if url.endswith("/auth/login"):
            return {"access_token": "admin_token"}
        if url.endswith("/admin/accounts"):
            assert kwargs["headers"]["Authorization"] == "Bearer admin_token"
            body = kwargs["json_body"]
            assert body["platform"] == "openai"
            assert body["type"] == "oauth"
            assert body["credentials"]["refresh_token"] == "ref"
            assert body["group_ids"] == [1, 2]
            return {"id": 99}
        raise AssertionError(url)

    provider = Sub2ApiExportProvider(
        Sub2ApiConfig(url="https://sub.example", email="admin@example.com", password="pw", group="1,2", model_whitelist="gpt-4.1", concurrency=5),
        request_json=fake_request,
    )
    record = OAuthExportRecord(
        account_id="acct_1",
        email="u@example.com",
        secret={"access_token": "acc", "refresh_token": "ref", "id_token": "", "client_id": "client_1"},
    )
    payload = provider.build_account_payload(record)
    assert payload["credentials"]["model_mapping"] == {"gpt-4.1": "gpt-4.1"}
    assert payload["concurrency"] == 5
    result = provider.push(record)
    assert result["remote_id"] == 99
    assert requests[0][1] == "https://sub.example/api/v1/auth/login"
