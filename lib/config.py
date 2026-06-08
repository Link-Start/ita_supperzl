# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

"""Configuration and .env loading."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    env_path = Path(path) if path else PROJECT_ROOT / ".env"
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def parse_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        out = int(float(str(value).strip()))
    except (TypeError, ValueError):
        out = default
    if minimum is not None:
        out = max(minimum, out)
    return out


def parse_float(value: Any, default: float, *, minimum: float | None = None) -> float:
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        out = default
    if minimum is not None:
        out = max(minimum, out)
    return out


def split_csv(value: Any) -> list[str]:
    text = str(value or "").replace("，", ",")
    result: list[str] = []
    seen: set[str] = set()
    for raw in text.replace(";", ",").replace("；", ",").split(","):
        item = raw.strip()
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


@dataclass
class RuntimeConfig:
    idp_base: str = "http://idp.fdvctte.info"
    idp_token: str = ""
    idp_client_id: str = ""
    idp_channel_id: str = ""
    idp_domain: str = ""
    idp_email: str = ""
    idp_given_name: str = ""
    idp_family_name: str = ""
    existing_account_id: str = ""

    codex_client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    codex_redirect_uri: str = "http://localhost:1455/auth/callback"
    codex_scope: str = "openid profile email offline_access"

    sub2api_url: str = ""
    sub2api_email: str = ""
    sub2api_password: str = ""
    sub2api_group: str = ""
    sub2api_model_whitelist: str = ""
    sub2api_concurrency: int = 10
    sub2api_priority: int = 1
    sub2api_rate_multiplier: float = 1.0

    artifact_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "artifacts" / "idp_codex")
    timeout: float = 30.0
    proxy: str = ""
    export_sub2api: bool = True

    @classmethod
    def from_env_and_args(cls, args: Any) -> "RuntimeConfig":
        load_dotenv()
        artifact = getattr(args, "artifact_dir", None) or env_first("ARTIFACT_DIR", default=str(PROJECT_ROOT / "artifacts" / "idp_codex"))
        proxy = getattr(args, "proxy", None) or env_first("HTTPS_PROXY", "HTTP_PROXY", default="")
        cfg = cls(
            idp_base=getattr(args, "idp_base", None) or env_first("IDP_BASE", default="http://idp.fdvctte.info"),
            idp_token=getattr(args, "idp_token", None) or env_first("IDP_TOKEN"),
            idp_client_id=getattr(args, "client_id", None) or env_first("IDP_CLIENT_ID"),
            idp_channel_id=str(getattr(args, "channel_id", None) or env_first("IDP_CHANNEL_ID")),
            idp_domain=getattr(args, "domain", None) or env_first("IDP_DOMAIN"),
            idp_email=getattr(args, "email", None) or "",
            idp_given_name=getattr(args, "given_name", None) or "",
            idp_family_name=getattr(args, "family_name", None) or "",
            existing_account_id=str(getattr(args, "account_id", None) or ""),
            codex_client_id=getattr(args, "codex_client_id", None) or env_first("CODEX_CLIENT_ID", default="app_EMoamEEZ73f0CkXaXp7hrann"),
            codex_redirect_uri=getattr(args, "codex_redirect_uri", None) or env_first("CODEX_REDIRECT_URI", default="http://localhost:1455/auth/callback"),
            codex_scope=getattr(args, "codex_scope", None) or env_first("CODEX_SCOPE", default="openid profile email offline_access"),
            sub2api_url=getattr(args, "sub2api_url", None) or env_first("SUB2API_URL", "CODEXFLOW_SUB2API_URL"),
            sub2api_email=getattr(args, "sub2api_email", None) or env_first("SUB2API_EMAIL", "CODEXFLOW_SUB2API_EMAIL"),
            sub2api_password=getattr(args, "sub2api_password", None) or env_first("SUB2API_PASSWORD", "CODEXFLOW_SUB2API_PASSWORD"),
            sub2api_group=getattr(args, "sub2api_group", None) or env_first("SUB2API_GROUP", "CODEXFLOW_SUB2API_GROUP"),
            sub2api_model_whitelist=getattr(args, "model_whitelist", None) or env_first("SUB2API_MODEL_WHITELIST", "CODEXFLOW_SUB2API_MODEL_WHITELIST"),
            sub2api_concurrency=parse_int(env_first("SUB2API_CONCURRENCY", "CODEXFLOW_SUB2API_CONCURRENCY", default="10"), 10, minimum=1),
            sub2api_priority=parse_int(env_first("SUB2API_PRIORITY", "CODEXFLOW_SUB2API_PRIORITY", default="1"), 1, minimum=1),
            sub2api_rate_multiplier=parse_float(env_first("SUB2API_RATE_MULTIPLIER", "CODEXFLOW_SUB2API_RATE_MULTIPLIER", default="1"), 1.0, minimum=0),
            artifact_dir=(PROJECT_ROOT / artifact) if not str(artifact).startswith("/") else Path(artifact),
            timeout=parse_float(getattr(args, "timeout", None) or env_first("REQUEST_TIMEOUT", default="30"), 30.0, minimum=1.0),
            proxy="" if getattr(args, "no_proxy", False) else proxy,
            export_sub2api=not bool(getattr(args, "no_sub2api", False)),
        )
        return cfg

    def validate(self) -> None:
        if not self.idp_token:
            raise ConfigError("缺少 IDP 访问码：请传 --idp-token 或设置 IDP_TOKEN", stage="config")
        if self.export_sub2api:
            missing = []
            if not self.sub2api_url:
                missing.append("SUB2API_URL")
            if not self.sub2api_email:
                missing.append("SUB2API_EMAIL")
            if not self.sub2api_password:
                missing.append("SUB2API_PASSWORD")
            if missing:
                raise ConfigError("缺少 Sub2API 配置：" + ", ".join(missing), stage="config")
