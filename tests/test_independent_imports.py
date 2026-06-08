# Copyright (c) 2026 Idp Team Automation.
# iDP 协议作者：@该隐；注册机作者：@朴圣佑。
# 二开请保留版权；二开不保留版权，以后写代码都是bug。

from __future__ import annotations

from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def test_no_external_project_runtime_imports_or_app_imports():
    forbidden = [
        "/Users/" + "leon.zhao" + "/Project/",
        "from " + "app.",
        "import " + "app.",
    ]
    for path in (PROJECT / "lib").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{path} contains forbidden marker {marker}"


def test_expected_tree_exists():
    expected = [
        ".env.example",
        ".gitignore",
        "README.md",
        "pyproject.toml",
        "input.example.json",
        "artifacts/.gitkeep",
        "lib/__init__.py",
        "lib/cli.py",
        "lib/config.py",
        "lib/idp_client.py",
        "lib/codex_oauth.py",
        "lib/sso_http_flow.py",
        "lib/sub2api_export.py",
        "lib/logging_utils.py",
        "lib/errors.py",
        "lib/batch_tui.py",
        "scripts/run_idp_codex.py",
        "scripts/run_batch_tui.py",
    ]
    for rel in expected:
        assert (PROJECT / rel).exists(), rel
