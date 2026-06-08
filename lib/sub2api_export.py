# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

"""Independent Sub2API OpenAI OAuth account export provider."""
from __future__ import annotations

import base64
import json
import socket
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .codex_oauth import DEFAULT_CLIENT_ID
from .config import parse_float, parse_int, split_csv
from .errors import ConfigError, Sub2ApiError
from .logging_utils import JsonlLogger, redact

RequestJson = Callable[..., Any]


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def parse_jwt_payload(token: str) -> dict[str, Any]:
    raw = str(token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def parse_timestamp(value: Any, *, default: int | None = None) -> int | None:
    if isinstance(value, (int, float)):
        return int(value if value < 1e11 else value / 1000)
    text = str(value or "").strip()
    if not text:
        return default
    try:
        numeric = float(text)
        return int(numeric if numeric < 1e11 else numeric / 1000)
    except Exception:
        pass
    try:
        if text.endswith("Z"):
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return default


@dataclass(frozen=True)
class Sub2ApiConfig:
    url: str
    email: str
    password: str
    group: str = ""
    model_whitelist: str = ""
    concurrency: int = 10
    priority: int = 1
    rate_multiplier: float = 1.0

    def validate(self) -> None:
        missing = []
        if not self.url:
            missing.append("SUB2API_URL")
        if not self.email:
            missing.append("SUB2API_EMAIL")
        if not self.password:
            missing.append("SUB2API_PASSWORD")
        if missing:
            raise ConfigError("缺少 Sub2API 配置：" + ", ".join(missing), stage="sub2api_config")


@dataclass(frozen=True)
class OAuthExportRecord:
    account_id: str
    email: str
    secret: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


class Sub2ApiExportProvider:
    name = "sub2api"

    def __init__(self, config: Sub2ApiConfig, *, logger: JsonlLogger | None = None, request_json: RequestJson | None = None):
        self.config = config
        self.config.validate()
        self.logger = logger
        self.request_json = request_json or self._request_json

    def _api_base(self, raw_url: str) -> str:
        base = str(raw_url or "").strip().rstrip("/")
        if not base:
            raise ConfigError("缺少 SUB2API_URL", stage="sub2api_config")
        if base.endswith("/api/v1"):
            return base
        if base.endswith("/api"):
            return f"{base}/v1"
        return f"{base}/api/v1"

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float = 15,
    ) -> Any:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        body = None if json_body is None else json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        default_headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 IdpTeamAutomation/0.1",
        }
        if origin:
            default_headers.setdefault("Origin", origin)
            default_headers.setdefault("Referer", f"{origin}/")
        merged_headers = {**default_headers, **(headers or {})}
        if self.logger:
            self.logger.write("sub2api_request", {"method": method, "url": url, "headers": merged_headers, "body": json_body})
        req = urllib.request.Request(url, data=body, method=method, headers=merged_headers)
        attempts = 3
        payload: Any = {}
        for attempt in range(1, attempts + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8", "replace")
                    payload = json.loads(text) if text.strip() else {}
                    if self.logger:
                        self.logger.write("sub2api_response", {"method": method, "url": url, "status": resp.status, "body": payload})
                break
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(text) if text.strip() else {}
                except Exception:
                    payload = {}
                if self.logger:
                    self.logger.write("sub2api_error", {"method": method, "url": url, "status": exc.code, "body": payload or text[:500]})
                message = payload.get("message") or payload.get("detail") or text[:200] or f"HTTP {exc.code}"
                raise Sub2ApiError(f"Sub2API request failed: {message}", stage="sub2api_request", data={"status": exc.code, "response": redact(payload)}) from exc
            except (urllib.error.URLError, ssl.SSLError, TimeoutError, socket.timeout, ConnectionResetError) as exc:
                if attempt >= attempts or not self._is_transient_request_error(exc):
                    if self.logger:
                        self.logger.write("sub2api_error", {"method": method, "url": url, "error": str(exc)})
                    raise Sub2ApiError(f"Sub2API request failed: {exc}", stage="sub2api_request", retryable=True) from exc
                time.sleep(min(5.0, 0.5 * attempt))
        if isinstance(payload, dict) and "code" in payload:
            if payload.get("code") in (0, "0"):
                return payload.get("data")
            raise Sub2ApiError(str(payload.get("message") or payload.get("detail") or payload), stage="sub2api_response")
        if isinstance(payload, dict) and set(payload) == {"data"}:
            return payload.get("data")
        return payload

    def _is_transient_request_error(self, exc: BaseException) -> bool:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (ssl.SSLEOFError, ssl.SSLError, TimeoutError, socket.timeout, ConnectionResetError)):
            return True
        text = str(reason or exc).lower()
        return any(marker in text for marker in ("ssl", "timed out", "connection reset", "connection aborted", "temporarily unavailable"))

    def _login(self, api_base: str) -> str:
        data = self.request_json("POST", f"{api_base}/auth/login", json_body={"email": self.config.email, "password": self.config.password}, timeout=15)
        if not isinstance(data, dict):
            raise Sub2ApiError("Sub2API 登录返回格式异常", stage="sub2api_login")
        token = first_non_empty(data.get("access_token"), data.get("token"), data.get("accessToken"))
        if not token:
            raise Sub2ApiError("Sub2API 登录成功但未返回 access_token", stage="sub2api_login")
        return token

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _group_ids(self) -> list[int]:
        ids: list[int] = []
        for item in split_csv(self.config.group):
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in ids:
                ids.append(value)
        return ids

    def _model_mapping(self) -> dict[str, str] | None:
        models = split_csv(self.config.model_whitelist)
        return {model: model for model in models} if models else None

    def build_credentials(self, record: OAuthExportRecord) -> dict[str, Any]:
        id_token = str(record.secret.get("id_token") or "").strip()
        claims = parse_jwt_payload(id_token)
        auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
        credentials: dict[str, Any] = {}
        for key in ("access_token", "refresh_token", "id_token"):
            value = str(record.secret.get(key) or "").strip()
            if value:
                credentials[key] = value
        expires_at = parse_timestamp(first_non_empty(record.secret.get("expired"), record.metadata.get("expired")))
        if expires_at:
            credentials["expires_at"] = expires_at
        client_id = first_non_empty(record.secret.get("client_id"), record.metadata.get("client_id"), claims.get("aud", [""])[0] if isinstance(claims.get("aud"), list) else "", DEFAULT_CLIENT_ID)
        if client_id:
            credentials["client_id"] = client_id
        email = first_non_empty(record.email, claims.get("email"), record.metadata.get("email"))
        if email:
            credentials["email"] = email
        remote_account_id = first_non_empty(record.secret.get("account_id"), record.metadata.get("remote_account_id"), auth_claims.get("chatgpt_account_id"), record.account_id)
        if remote_account_id:
            credentials["chatgpt_account_id"] = remote_account_id
        user_id = first_non_empty(auth_claims.get("chatgpt_user_id"), auth_claims.get("user_id"), record.secret.get("user_id"))
        if user_id:
            credentials["chatgpt_user_id"] = user_id
        plan_type = first_non_empty(auth_claims.get("chatgpt_plan_type"), record.metadata.get("plan_type"))
        if plan_type:
            credentials["plan_type"] = plan_type
        mapping = self._model_mapping()
        if mapping:
            credentials["model_mapping"] = mapping
        return credentials

    def build_account_payload(self, record: OAuthExportRecord) -> dict[str, Any]:
        credentials = self.build_credentials(record)
        if not first_non_empty(credentials.get("access_token"), credentials.get("refresh_token")):
            raise Sub2ApiError("Sub2API 导出缺少 access_token/refresh_token", stage="sub2api_payload")
        email = first_non_empty(record.email, credentials.get("email"), record.account_id)
        return {
            "name": str(email)[:64],
            "platform": "openai",
            "type": "oauth",
            "credentials": credentials,
            "extra": {
                "idp_team_automation_managed": True,
                "idp_team_automation_source": "idp_codex",
                "idp_team_automation_codex_managed": True,
                "idp_team_automation_codex_source": "idp_team_automation",
                "idp_team_automation_email": str(email).lower(),
                "email": str(email).lower(),
            },
            "concurrency": parse_int(self.config.concurrency, 10, minimum=1),
            "priority": parse_int(self.config.priority, 1, minimum=1),
            "rate_multiplier": parse_float(self.config.rate_multiplier, 1.0, minimum=0),
            "auto_pause_on_expired": True,
            "group_ids": self._group_ids(),
        }

    def push(self, record: OAuthExportRecord) -> dict[str, Any]:
        api_base = self._api_base(self.config.url)
        token = self._login(api_base)
        payload = self.build_account_payload(record)
        created = self.request_json("POST", f"{api_base}/admin/accounts", headers=self._auth_headers(token), json_body=payload, timeout=20)
        remote_id = created.get("id") if isinstance(created, dict) else ""
        return {
            "status": "success",
            "provider": self.name,
            "email": record.email,
            "account_id": record.account_id,
            "remote_id": remote_id,
            "remote_action": "created",
        }
