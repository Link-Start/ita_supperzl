# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

"""Command-line orchestration for IDP -> Codex OAuth -> Sub2API."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .codex_oauth import generate_oauth_start, public_token_result
from .config import RuntimeConfig
from .errors import IdpTeamAutomationError
from .idp_client import IdpClient
from .logging_utils import JsonlLogger, redact, utc_now_iso
from .sso_http_flow import SSOHttpFlow
from .sub2api_export import OAuthExportRecord, Sub2ApiConfig, Sub2ApiExportProvider

ProgressFn = Callable[[str, dict[str, Any] | None], None]


def _progress(message: str, data: dict[str, Any] | None = None) -> None:
    """Print a redacted, human-readable progress line to stderr.

    stdout remains reserved for the final machine-readable JSON result.
    """
    suffix = ""
    if data:
        safe = redact(data)
        visible = {k: v for k, v in safe.items() if v not in ("", None, [], {})}
        if visible:
            suffix = " " + json.dumps(visible, ensure_ascii=False, sort_keys=True, default=str)
    print(f"[{utc_now_iso()}] {message}{suffix}", file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IDP 生成账号 -> Codex OAuth refresh token -> Sub2API 推送")
    parser.add_argument("--idp-base", help="IDP base URL，默认读取 IDP_BASE 或 http://idp.fdvctte.info")
    parser.add_argument("--idp-token", help="IDP 访问码")
    parser.add_argument("--client-id", help="IDP client_id，例如 openai-client3")
    parser.add_argument("--channel-id", help="IDP channel_id；不填则随机/按 client_id")
    parser.add_argument("--domain", help="邮箱后缀；不填由 IDP 选择")
    parser.add_argument("--email", help="邮箱或前缀；不填由 IDP 生成")
    parser.add_argument("--given-name", help="名；不填随机")
    parser.add_argument("--family-name", help="姓；不填随机")
    parser.add_argument("--account-id", help="复用已生成的 IDP account_id，避免再次生成扣点")

    parser.add_argument("--codex-client-id", help="Codex OAuth client_id")
    parser.add_argument("--codex-redirect-uri", help="Codex OAuth redirect_uri")
    parser.add_argument("--codex-scope", help="Codex OAuth scope")

    parser.add_argument("--sub2api-url", help="Sub2API base URL")
    parser.add_argument("--sub2api-email", help="Sub2API 管理员邮箱")
    parser.add_argument("--sub2api-password", help="Sub2API 管理员密码")
    parser.add_argument("--sub2api-group", help="Sub2API 分组 ID，多个用逗号")
    parser.add_argument("--model-whitelist", help="Sub2API model whitelist，多个用逗号")
    parser.add_argument("--no-sub2api", action="store_true", help="只获取 token，不推送 Sub2API")
    parser.add_argument("--artifact-dir", help="artifact 输出目录，默认 artifacts/idp_codex")
    parser.add_argument("--timeout", help="HTTP timeout 秒数")
    parser.add_argument("--proxy", help="HTTP/HTTPS proxy")
    parser.add_argument("--no-proxy", action="store_true", help="禁用 proxy")
    return parser


def _write_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")


def run(cfg: RuntimeConfig, *, progress: ProgressFn | None = _progress) -> dict[str, Any]:
    cfg.validate()
    cfg.artifact_dir.mkdir(parents=True, exist_ok=True)
    logger = JsonlLogger(cfg.artifact_dir / "network.jsonl")
    if progress:
        progress("步骤 1/10：初始化运行环境", {
            "artifact_dir": str(cfg.artifact_dir),
            "idp_base": cfg.idp_base,
            "export_sub2api": cfg.export_sub2api,
        })
    logger.write("run_start", {"artifact_dir": str(cfg.artifact_dir), "idp_base": cfg.idp_base, "client_id": cfg.idp_client_id, "export_sub2api": cfg.export_sub2api})

    idp = IdpClient(cfg.idp_base, timeout=cfg.timeout, logger=logger)
    if progress:
        progress("步骤 2/10：读取 IDP bootstrap/channel 配置", {"client_id": cfg.idp_client_id})
    bootstrap = idp.bootstrap(client_id=cfg.idp_client_id)
    _write_json(cfg.artifact_dir / "bootstrap.json", redact(bootstrap))

    if progress:
        progress("步骤 3/10：校验 IDP 访问码并读取账号列表", None)
    me = idp.me(token=cfg.idp_token, client_id=cfg.idp_client_id)
    _write_json(cfg.artifact_dir / "me.json", redact(me))

    if cfg.existing_account_id:
        if progress:
            progress("步骤 4/10：复用已生成账号", {"account_id": cfg.existing_account_id})
        selected = None
        items = ((me.get("accounts") or {}).get("items") or []) if isinstance(me, dict) else []
        for item in items:
            if str(item.get("id") or "") == str(cfg.existing_account_id):
                selected = item
                break
        if not selected:
            selected = {"id": cfg.existing_account_id, "email": cfg.idp_email or "", "password": ""}
        from .idp_client import GeneratedAccount
        account = GeneratedAccount.from_payload(selected)
    else:
        if progress:
            progress("步骤 4/10：生成新账号", {
                "channel_id": cfg.idp_channel_id,
                "client_id": cfg.idp_client_id,
                "domain": cfg.idp_domain,
            })
        account = idp.generate_account(
            token=cfg.idp_token,
            channel_id=cfg.idp_channel_id,
            client_id=cfg.idp_client_id,
            domain=cfg.idp_domain,
            email=cfg.idp_email,
            given_name=cfg.idp_given_name,
            family_name=cfg.idp_family_name,
        )
    _write_json(cfg.artifact_dir / "account.public.json", account.as_public_dict())
    if progress:
        progress("账号已准备", account.as_public_dict())

    if progress:
        progress("步骤 5/10：启动 IDP SSO 会话", {"account_id": account.id})
    start_url = idp.start_sso(token=cfg.idp_token, account_id=account.id)
    _write_json(cfg.artifact_dir / "sso_start.public.json", {"start_url": redact(start_url), "account": account.as_public_dict()})

    if progress:
        progress("步骤 6/10：生成 Codex OAuth/PKCE 授权 URL", {
            "client_id": cfg.codex_client_id,
            "redirect_uri": cfg.codex_redirect_uri,
        })
    oauth = generate_oauth_start(
        redirect_uri=cfg.codex_redirect_uri,
        client_id=cfg.codex_client_id,
        scope=cfg.codex_scope,
    )
    _write_json(cfg.artifact_dir / "oauth_start.public.json", {
        "auth_url": redact(oauth.auth_url),
        "state": "***REDACTED***",
        "redirect_uri": oauth.redirect_uri,
        "client_id": oauth.client_id,
        "scope": oauth.scope,
    })

    flow = SSOHttpFlow(timeout=cfg.timeout, proxy=cfg.proxy, artifact_dir=cfg.artifact_dir, logger=logger, user_token=cfg.idp_token)
    if progress:
        progress("步骤 7/10：执行纯 HTTP 协议 SSO + Codex OAuth 授权流程", None)
    token_config = flow.run(start_url=start_url, oauth=oauth, account=account)
    token_public = public_token_result(token_config)
    _write_json(cfg.artifact_dir / "token.public.json", token_public)
    if progress:
        progress("步骤 8/10：Codex token 获取完成", token_public)

    sub2api_result: dict[str, Any] | None = None
    if cfg.export_sub2api:
        if progress:
            progress("步骤 9/10：推送账号到 Sub2API", {"email": token_public.get("email") or account.email})
        record = OAuthExportRecord(
            account_id=str(token_config.get("account_id") or account.email),
            email=str(token_config.get("email") or account.email),
            secret={
                "access_token": token_config.get("access_token") or "",
                "refresh_token": token_config.get("refresh_token") or "",
                "id_token": token_config.get("id_token") or "",
                "client_id": token_config.get("client_id") or cfg.codex_client_id,
                "expired": token_config.get("expired") or "",
                "account_id": token_config.get("account_id") or "",
                "user_id": token_config.get("user_id") or "",
            },
            metadata={"email": account.email, "source": "idp_codex", "generated_account_id": account.id},
        )
        provider = Sub2ApiExportProvider(
            Sub2ApiConfig(
                url=cfg.sub2api_url,
                email=cfg.sub2api_email,
                password=cfg.sub2api_password,
                group=cfg.sub2api_group,
                model_whitelist=cfg.sub2api_model_whitelist,
                concurrency=cfg.sub2api_concurrency,
                priority=cfg.sub2api_priority,
                rate_multiplier=cfg.sub2api_rate_multiplier,
            ),
            logger=logger,
        )
        sub2api_result = provider.push(record)
        _write_json(cfg.artifact_dir / "sub2api.public.json", sub2api_result)
        if progress:
            progress("Sub2API 推送完成", sub2api_result)
    else:
        if progress:
            progress("步骤 9/10：跳过 Sub2API 推送", {"reason": "--no-sub2api"})

    if progress:
        progress("步骤 10/10：写入最终结果文件", None)
    result = {
        "status": "success",
        "finished_at": utc_now_iso(),
        "artifact_dir": str(cfg.artifact_dir),
        "account": account.as_public_dict(),
        "token": token_public,
        "sub2api": sub2api_result,
    }
    _write_json(cfg.artifact_dir / "result.json", result)
    logger.write("run_success", result)
    if progress:
        progress("全部完成", {"artifact_dir": str(cfg.artifact_dir), "status": "success"})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = RuntimeConfig.from_env_and_args(args)
        result = run(cfg)
    except IdpTeamAutomationError as exc:
        payload = {"status": "failed", "stage": exc.stage, "error": str(exc), "retryable": exc.retryable, "data": redact(exc.data)}
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), file=sys.stderr)
        return 1
    except Exception as exc:
        payload = {"status": "failed", "stage": "unexpected", "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
