# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

"""HTTP client for the account-generation IDP web app."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .errors import IdpError
from .logging_utils import JsonlLogger, redact

RequestJson = Callable[[str, str, dict[str, Any] | None], Any]


@dataclass(frozen=True)
class GeneratedAccount:
    id: int
    email: str
    password: str
    name: str = ""
    given_name: str = ""
    family_name: str = ""
    channel_id: str = ""
    channel_name: str = ""
    raw: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GeneratedAccount":
        data = payload.get("account") if isinstance(payload.get("account"), dict) else payload
        if not isinstance(data, dict):
            raise IdpError("IDP 生成账号返回格式异常", stage="idp_generate")
        email = str(data.get("email") or "").strip()
        password = str(data.get("password") or data.get("pass") or "").strip()
        if not email:
            raise IdpError("IDP 生成账号未返回 email", stage="idp_generate", data={"response": redact(payload)})
        try:
            account_id = int(data.get("id") or 0)
        except (TypeError, ValueError):
            account_id = 0
        return cls(
            id=account_id,
            email=email,
            password=password,
            name=str(data.get("name") or "").strip(),
            given_name=str(data.get("given_name") or data.get("given") or "").strip(),
            family_name=str(data.get("family_name") or data.get("family") or "").strip(),
            channel_id=str(data.get("channel_id") or "").strip(),
            channel_name=str(data.get("channel_name") or "").strip(),
            raw=data,
        )

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "has_password": bool(self.password),
        }


class IdpClient:
    def __init__(
        self,
        base_url: str = "http://idp.fdvctte.info",
        *,
        timeout: float = 30,
        logger: JsonlLogger | None = None,
        request_json: RequestJson | None = None,
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = timeout
        self.logger = logger
        self.request_json = request_json or self._request_json

    def _request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 IdpTeamAutomation/0.1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.logger:
            self.logger.write("idp_request", {"method": method, "url": url, "body": body or {}})
        import time
        attempts = 4
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    text = resp.read().decode("utf-8", "replace")
                    payload = json.loads(text) if text.strip() else {}
                    if self.logger:
                        self.logger.write("idp_response", {"method": method, "url": url, "status": resp.status, "attempt": attempt, "body": payload})
                    return payload
            except urllib.error.HTTPError as exc:
                last_exc = exc
                text = exc.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(text) if text.strip() else {}
                except Exception:
                    payload = {"raw": text[:500]}
                if self.logger:
                    self.logger.write("idp_error", {"method": method, "url": url, "status": exc.code, "attempt": attempt, "body": payload})
                if exc.code >= 500 and attempt < attempts:
                    time.sleep(min(3.0, 0.5 * attempt))
                    continue
                msg = payload.get("error") or payload.get("message") or text[:200] or f"HTTP {exc.code}"
                raise IdpError(f"IDP 请求失败：{msg}", stage="idp_request", retryable=exc.code >= 500, data={"status": exc.code, "response": redact(payload)}) from exc
            except Exception as exc:
                last_exc = exc
                if self.logger:
                    self.logger.write("idp_error", {"method": method, "url": url, "attempt": attempt, "error": str(exc)})
                if attempt < attempts:
                    time.sleep(min(3.0, 0.5 * attempt))
                    continue
                raise IdpError(f"IDP 请求异常：{exc}", stage="idp_request", retryable=True) from exc
        raise IdpError(f"IDP 请求异常：{last_exc}", stage="idp_request", retryable=True)

    def bootstrap(self, *, client_id: str = "") -> dict[str, Any]:
        path = "/api/user/bootstrap"
        if client_id:
            path += "?" + urllib.parse.urlencode({"client_id": client_id})
        data = self.request_json("GET", path, None)
        if not isinstance(data, dict):
            raise IdpError("IDP bootstrap 返回格式异常", stage="idp_bootstrap")
        return data

    def me(self, *, token: str, page: int = 1, page_size: int = 20, client_id: str = "", q: str = "") -> dict[str, Any]:
        data = self.request_json("POST", "/api/user/me", {
            "token": token,
            "page": page,
            "page_size": page_size,
            "client_id": client_id,
            "q": q,
        })
        if not isinstance(data, dict):
            raise IdpError("IDP me 返回格式异常", stage="idp_me")
        return data

    def generate_account(
        self,
        *,
        token: str,
        channel_id: int | str = 0,
        client_id: str = "",
        domain: str = "",
        email: str = "",
        given_name: str = "",
        family_name: str = "",
    ) -> GeneratedAccount:
        try:
            normalized_channel_id = int(channel_id or 0)
        except (TypeError, ValueError):
            normalized_channel_id = 0
        payload = self.request_json("POST", "/api/user/generate", {
            "token": token,
            "channel_id": normalized_channel_id,
            "client_id": client_id,
            "domain": domain,
            "email": email,
            "given_name": given_name,
            "family_name": family_name,
        })
        if not isinstance(payload, dict):
            raise IdpError("IDP generate 返回格式异常", stage="idp_generate")
        return GeneratedAccount.from_payload(payload)

    def start_sso(self, *, token: str, account_id: int | str) -> str:
        payload = self.request_json("POST", "/api/user/start-sso", {"token": token, "account_id": account_id})
        if not isinstance(payload, dict):
            raise IdpError("IDP start-sso 返回格式异常", stage="idp_start_sso")
        start_url = str(payload.get("start_url") or "").strip()
        if not start_url:
            raise IdpError("IDP start-sso 未返回 start_url", stage="idp_start_sso", data={"response": redact(payload)})
        return start_url
