# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

"""Project-specific exceptions."""
from __future__ import annotations

from typing import Any


class IdpTeamAutomationError(RuntimeError):
    """Base exception for this project."""

    def __init__(self, message: str, *, stage: str = "", retryable: bool = False, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable
        self.data = data or {}


class ConfigError(IdpTeamAutomationError):
    """Configuration is missing or invalid."""


class IdpError(IdpTeamAutomationError):
    """IDP request or response failed."""


class OAuthFlowError(IdpTeamAutomationError):
    """Codex OAuth/SSO flow failed."""


class Sub2ApiError(IdpTeamAutomationError):
    """Sub2API request or response failed."""
