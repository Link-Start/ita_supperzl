# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

"""Interactive batch runner with a lightweight terminal dashboard."""
from __future__ import annotations

import argparse
import json
import queue
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .cli import run as run_single
from .config import PROJECT_ROOT, RuntimeConfig, parse_int
from .errors import IdpTeamAutomationError
from .logging_utils import redact, utc_now_iso


@dataclass
class TaskState:
    index: int
    status: str = "PENDING"
    message: str = "等待中"
    email: str = ""
    account_id: str = ""
    remote_id: str = ""
    artifact_dir: str = ""
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    updated_at: float = field(default_factory=time.time)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量生成 IDP 账号 -> Codex OAuth -> Sub2API，多线程 TUI")
    parser.add_argument("--count", type=int, help="需要生成的账号数量；不传则进入交互输入")
    parser.add_argument("--threads", type=int, help="并发线程数；不传则进入交互输入")
    parser.add_argument("--yes", action="store_true", help="跳过启动确认")

    parser.add_argument("--idp-base", help="IDP base URL")
    parser.add_argument("--idp-token", help="IDP 访问码")
    parser.add_argument("--client-id", help="IDP client_id")
    parser.add_argument("--channel-id", help="IDP channel_id")
    parser.add_argument("--domain", help="邮箱后缀")

    parser.add_argument("--codex-client-id", help="Codex OAuth client_id")
    parser.add_argument("--codex-redirect-uri", help="Codex OAuth redirect_uri")
    parser.add_argument("--codex-scope", help="Codex OAuth scope")

    parser.add_argument("--sub2api-url", help="Sub2API base URL")
    parser.add_argument("--sub2api-email", help="Sub2API 管理员邮箱")
    parser.add_argument("--sub2api-password", help="Sub2API 管理员密码")
    parser.add_argument("--sub2api-group", help="Sub2API 分组 ID，多个用逗号")
    parser.add_argument("--model-whitelist", help="Sub2API model whitelist，多个用逗号")
    parser.add_argument("--no-sub2api", action="store_true", help="只获取 token，不推送 Sub2API")

    parser.add_argument("--artifact-dir", help="批量 artifact 根目录；默认 artifacts/batch_<timestamp>")
    parser.add_argument("--retries", type=int, default=5, help="每个任务失败重试次数，默认 5")
    parser.add_argument("--timeout", help="HTTP timeout 秒数")
    parser.add_argument("--proxy", help="HTTP/HTTPS proxy")
    parser.add_argument("--no-proxy", action="store_true", help="禁用 proxy")
    return parser


def _prompt_int(label: str, *, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    while True:
        suffix = f" [{default}]"
        value = input(f"{label}{suffix}: ").strip()
        number = default if not value else parse_int(value, default, minimum=minimum)
        if maximum is not None and number > maximum:
            print(f"请输入不超过 {maximum} 的数字")
            continue
        if number >= minimum:
            return number


def _prompt_yes_no(label: str, *, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{label} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "1", "true", "是", "好"}


def _config_namespace(args: argparse.Namespace, artifact_dir: Path, *, no_sub2api: bool) -> SimpleNamespace:
    return SimpleNamespace(
        idp_base=args.idp_base,
        idp_token=args.idp_token,
        client_id=args.client_id,
        channel_id=args.channel_id,
        domain=args.domain,
        email="",
        given_name="",
        family_name="",
        account_id="",
        codex_client_id=args.codex_client_id,
        codex_redirect_uri=args.codex_redirect_uri,
        codex_scope=args.codex_scope,
        sub2api_url=args.sub2api_url,
        sub2api_email=args.sub2api_email,
        sub2api_password=args.sub2api_password,
        sub2api_group=args.sub2api_group,
        model_whitelist=args.model_whitelist,
        no_sub2api=no_sub2api,
        artifact_dir=str(artifact_dir),
        timeout=args.timeout,
        proxy=args.proxy,
        no_proxy=bool(args.no_proxy),
    )


def _task_progress(events: "queue.Queue[dict[str, Any]]", index: int):
    def emit(message: str, data: dict[str, Any] | None = None) -> None:
        events.put({"type": "progress", "index": index, "message": message, "data": redact(data or {}), "ts": utc_now_iso()})

    return emit


def _run_one(index: int, base_cfg: RuntimeConfig, artifact_root: Path, events: "queue.Queue[dict[str, Any]]", *, retries: int) -> None:
    max_attempts = max(1, int(retries or 1))
    last_failure: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        task_dir = artifact_root / f"task_{index:04d}" / f"attempt_{attempt:02d}"
        cfg = replace(
            base_cfg,
            existing_account_id="",
            idp_email="",
            idp_given_name="",
            idp_family_name="",
            artifact_dir=task_dir,
        )
        events.put({"type": "started", "index": index, "attempt": attempt, "max_attempts": max_attempts, "artifact_dir": str(task_dir), "ts": utc_now_iso()})
        try:
            result = run_single(cfg, progress=_task_progress(events, index))
            events.put({"type": "success", "index": index, "attempt": attempt, "result": redact(result), "ts": utc_now_iso()})
            return
        except IdpTeamAutomationError as exc:
            last_failure = {
                "type": "attempt_failed",
                "index": index,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "stage": exc.stage,
                "error": str(exc),
                "retryable": exc.retryable,
                "data": redact(exc.data),
                "ts": utc_now_iso(),
            }
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            last_failure = {
                "type": "attempt_failed",
                "index": index,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "stage": "unexpected",
                "error": str(exc),
                "retryable": False,
                "data": {},
                "ts": utc_now_iso(),
            }
        events.put(last_failure)
        if attempt < max_attempts:
            time.sleep(min(5.0, 0.8 * attempt))
    final = dict(last_failure)
    final["type"] = "failed"
    final["ts"] = utc_now_iso()
    events.put(final)


def _apply_event(states: dict[int, TaskState], event: dict[str, Any], recent: list[str]) -> None:
    idx = int(event.get("index") or 0)
    if idx not in states:
        return
    state = states[idx]
    kind = str(event.get("type") or "")
    state.updated_at = time.time()
    if kind == "started":
        state.status = "RUNNING"
        state.message = f"第 {event.get('attempt')}/{event.get('max_attempts')} 次尝试已启动"
        state.started_at = str(event.get("ts") or "")
        state.artifact_dir = str(event.get("artifact_dir") or "")
    elif kind == "progress":
        state.status = "RUNNING"
        state.message = str(event.get("message") or "")[:80]
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if data.get("email"):
            state.email = str(data.get("email") or "")
        if data.get("id") and "账号已准备" in state.message:
            state.account_id = str(data.get("id") or "")
    elif kind == "success":
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        account = result.get("account") if isinstance(result.get("account"), dict) else {}
        sub2api = result.get("sub2api") if isinstance(result.get("sub2api"), dict) else {}
        state.status = "SUCCESS"
        state.message = f"完成，第 {event.get('attempt')} 次尝试成功"
        state.finished_at = str(event.get("ts") or "")
        state.email = str(account.get("email") or result.get("email") or state.email)
        state.account_id = str(account.get("id") or state.account_id)
        state.remote_id = str(sub2api.get("remote_id") or "")
    elif kind == "attempt_failed":
        state.status = "RUNNING"
        state.message = f"第 {event.get('attempt')}/{event.get('max_attempts')} 次失败：{event.get('stage') or 'failed'}"
        state.error = str(event.get("error") or "")[:240]
    elif kind == "failed":
        state.status = "FAILED"
        state.message = f"{event.get('max_attempts') or 5} 次重试失败：{event.get('stage') or 'failed'}"
        state.finished_at = str(event.get("ts") or "")
        state.error = str(event.get("error") or "")[:240]
    if kind in {"progress", "success", "attempt_failed", "failed"}:
        recent.append(f"[{event.get('ts')}] #{idx:04d} {state.status} {state.message}")
        del recent[:-12]


def _status_counts(states: dict[int, TaskState]) -> dict[str, int]:
    counts = {"PENDING": 0, "RUNNING": 0, "SUCCESS": 0, "FAILED": 0}
    for state in states.values():
        counts[state.status] = counts.get(state.status, 0) + 1
    return counts


def _render(states: dict[int, TaskState], recent: list[str], *, artifact_root: Path, count: int, threads: int) -> None:
    width, height = shutil.get_terminal_size((120, 30))
    counts = _status_counts(states)
    running = [state for state in states.values() if state.status == "RUNNING"]
    failed = [state for state in states.values() if state.status == "FAILED"]
    succeeded = [state for state in states.values() if state.status == "SUCCESS"]

    def row(state: TaskState) -> str:
        email = (state.email[:31] + "...") if len(state.email) > 34 else state.email
        msg = state.message if not state.error else f"{state.message}: {state.error}"
        return f"{state.index:>4} {state.status:<8} {state.account_id:<8} {email:<34} {state.remote_id:<8}  {msg[:max(10, width - 70)]}"

    lines = [
        "\033[2J\033[H",
        "Idp Team Automation 批量生成 TUI",
        f"目标: {count} | 线程: {threads} | 成功: {counts['SUCCESS']} | 失败: {counts['FAILED']} | 运行中: {counts['RUNNING']} | 等待: {counts['PENDING']}",
        f"Artifacts: {artifact_root}",
        "-" * min(width, 140),
        "运行中任务:",
        f"{'#':>4} {'状态':<8} {'账号ID':<8} {'邮箱':<34} {'Sub2API':<8}  当前步骤",
        "-" * min(width, 140),
    ]
    if running:
        for state in running:
            lines.append(row(state))
    else:
        lines.append("暂无运行中任务")

    spare_rows = max(6, height - len(lines) - 8)
    success_rows = max(0, min(5, spare_rows // 2))
    failed_rows = max(0, min(5, spare_rows - success_rows))

    if succeeded:
        lines.extend(["-" * min(width, 140), f"最近成功任务（显示 {min(success_rows, len(succeeded))}/{len(succeeded)}）:"])
        for state in succeeded[-success_rows:] if success_rows else []:
            lines.append(row(state))

    if failed:
        lines.extend(["-" * min(width, 140), f"失败任务（显示 {min(failed_rows, len(failed))}/{len(failed)}）:"])
        for state in failed[-failed_rows:] if failed_rows else []:
            lines.append(row(state))

    lines.extend(["-" * min(width, 140), "最近事件:"])
    lines.extend(recent[-6:])
    print("\n".join(lines), end="", flush=True)


def _write_summary(artifact_root: Path, states: dict[int, TaskState], *, count: int, threads: int, retries: int) -> dict[str, Any]:
    counts = _status_counts(states)
    status = "success" if counts["FAILED"] == 0 and counts["SUCCESS"] == count else "partial_failed"
    summary = {
        "status": status,
        "finished_at": utc_now_iso(),
        "count": count,
        "threads": threads,
        "retries": retries,
        "artifact_dir": str(artifact_root),
        "success_count": counts["SUCCESS"],
        "failed_count": counts["FAILED"],
        "tasks": [
            {
                "index": state.index,
                "status": state.status,
                "email": state.email,
                "account_id": state.account_id,
                "remote_id": state.remote_id,
                "artifact_dir": state.artifact_dir,
                "error": state.error,
            }
            for state in states.values()
        ],
    }
    artifact_root.mkdir(parents=True, exist_ok=True)
    (artifact_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def _print_summary(summary: dict[str, Any]) -> None:
    print()
    print("批量执行完成")
    print("=" * 40)
    print(f"总任务数: {summary.get('count')}")
    print(f"成功数量: {summary.get('success_count')}")
    print(f"失败数量: {summary.get('failed_count')}")
    print(f"线程数量: {summary.get('threads')}")
    print(f"单任务最大重试: {summary.get('retries')}")
    print(f"结果目录: {summary.get('artifact_dir')}")
    print(f"统计文件: {summary.get('artifact_dir')}/summary.json")

    failed = [item for item in summary.get("tasks", []) if isinstance(item, dict) and item.get("status") == "FAILED"]
    if failed:
        print()
        print("失败任务:")
        for item in failed[:20]:
            print(f"- #{int(item.get('index') or 0):04d} account={item.get('account_id') or '-'} email={item.get('email') or '-'} error={item.get('error') or '-'}")
        if len(failed) > 20:
            print(f"... 还有 {len(failed) - 20} 个失败任务，详见 summary.json")


def run_batch(base_cfg: RuntimeConfig, *, count: int, threads: int, artifact_root: Path, retries: int = 5) -> dict[str, Any]:
    artifact_root.mkdir(parents=True, exist_ok=True)
    states = {i: TaskState(index=i) for i in range(1, count + 1)}
    events: "queue.Queue[dict[str, Any]]" = queue.Queue()
    recent: list[str] = []
    stop_render = threading.Event()

    def drain() -> None:
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            _apply_event(states, event, recent)

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(_run_one, i, base_cfg, artifact_root, events, retries=retries) for i in range(1, count + 1)]
        while not stop_render.is_set():
            drain()
            if sys.stdout.isatty():
                _render(states, recent, artifact_root=artifact_root, count=count, threads=threads)
            done = sum(1 for future in futures if future.done())
            if done == len(futures):
                break
            time.sleep(0.25)
        for future in futures:
            future.result()
    drain()
    if sys.stdout.isatty():
        _render(states, recent, artifact_root=artifact_root, count=count, threads=threads)
        print()
    return _write_summary(artifact_root, states, count=count, threads=threads, retries=max(1, int(retries or 1)))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    interactive = sys.stdin.isatty() and (args.count is None or args.threads is None)
    if interactive:
        print("Idp Team Automation 批量生成 TUI")
        print("说明：每个账号会独立生成、授权并写入独立 artifact；真实运行会消耗 IDP 点数。")
        args.count = _prompt_int("需要生成的账号数量", default=args.count or 1, minimum=1)
        args.threads = _prompt_int("启动线程数", default=args.threads or min(3, args.count), minimum=1, maximum=args.count)
        args.no_sub2api = not _prompt_yes_no("是否推送到 Sub2API", default=not args.no_sub2api)
    elif args.count is None or args.threads is None:
        parser.error("非交互环境必须传 --count 和 --threads")

    count = max(1, int(args.count or 1))
    threads = max(1, min(int(args.threads or 1), count))
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    artifact_root = Path(args.artifact_dir) if args.artifact_dir else PROJECT_ROOT / "artifacts" / f"batch_{timestamp}"
    if not artifact_root.is_absolute():
        artifact_root = PROJECT_ROOT / artifact_root

    if not args.yes:
        print(f"即将生成 {count} 个账号，并发线程 {threads}，artifact: {artifact_root}")
        print(f"Sub2API: {'关闭' if args.no_sub2api else '开启'}；纯协议失败重试: {max(1, int(args.retries or 5))} 次")
        if not _prompt_yes_no("确认启动", default=False):
            print("已取消")
            return 2

    cfg_args = _config_namespace(args, artifact_root, no_sub2api=bool(args.no_sub2api))
    try:
        base_cfg = RuntimeConfig.from_env_and_args(cfg_args)
        base_cfg.validate()
        summary = run_batch(base_cfg, count=count, threads=threads, artifact_root=artifact_root, retries=max(1, int(args.retries or 5)))
    except IdpTeamAutomationError as exc:
        payload = {"status": "failed", "stage": exc.stage, "error": str(exc), "retryable": exc.retryable, "data": redact(exc.data)}
        print(f"启动失败: stage={payload['stage']} error={payload['error']}", file=sys.stderr)
        return 1
    _print_summary(summary)
    return 0 if summary.get("status") == "success" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
