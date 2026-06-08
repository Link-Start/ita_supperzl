# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

"""Safe JSONL logging helpers with secret redaction."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SENSITIVE_MARKERS = (
    "authorization",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "password",
    "secret",
    "cookie",
    "code",
    "otp",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_sensitive_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(marker in lowered for marker in SENSITIVE_MARKERS)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[str(key)] = "***REDACTED***" if is_sensitive_key(str(key)) else redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        # Hide common inline secret patterns without destroying ordinary URLs.
        text = value
        text = re.sub(r"(?i)(access_token|refresh_token|id_token|token|password|authorization|cookie|code)=([^&\s]+)", r"\1=***REDACTED***", text)
        text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]+", r"\1***REDACTED***", text)
        return text
    return value


class JsonlLogger:
    """Append-only JSONL logger used for artifacts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, payload: dict[str, Any] | None = None) -> None:
        item = {"ts": utc_now_iso(), "event": event, **redact(payload or {})}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) + "\n")
