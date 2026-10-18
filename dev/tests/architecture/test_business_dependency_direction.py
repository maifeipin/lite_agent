"""
Codex Review Round 7 Closeout Probes (2026-10-16).
验证第 7 轮复核报告中 1-4 项整改的边界闭环：
1. 高优邮件通知宿主注入与真实投递；
2. 公共 channels 彻底解除 features 反向依赖并纳管门禁；
3. 清退 architecture_rules.json 中 12 条 kernel_stage_2 账单依赖债务；
4. 会议业务主体归位 features/review_meeting/room.py、薄 CLI、request_selector 业务路由数据交由插件提供。
"""

import ast
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = ROOT / "lite_agent" if (ROOT / "lite_agent").is_dir() else ROOT


def test_mail_high_priority_notification_injected_to_host_agent():
    """Item 1: 邮件高优告警通知正确携带宿主 Agent 实例并真实触发投递。"""
    from lite_agent.features.mail_ops import register
    from lite_agent.contracts.message import IncomingMessage
    from lite_agent.services.commands import CommandRegistry

    cmd_reg = CommandRegistry()
    mock_agent = SimpleNamespace(
        _config={},
        channels=[],
    )
    mock_context = SimpleNamespace(
        agent=mock_agent,
        config={},
        command_registry=cmd_reg,
        register_command=cmd_reg.register,
        register_skill=lambda *a, **kw: None,
        register_route=lambda *a, **kw: None,
        register_cron=lambda *a, **kw: None,
        register_domain=lambda *a, **kw: None,
    )
    register(mock_context)

    mock_msg = IncomingMessage(
        channel="feishu",
        user_id="admin_1",
        chat_id="chat_1",
        message_id="msg_1",
        text="/mail_fetch",
        is_guest=False,
    )

    with pytest.MonkeyPatch.context() as mp:
        recorded_agent = []

        def fake_mail_fetch(months=1, agent=None):
            recorded_agent.append(agent)
            return "fetched"

        mp.setattr("lite_agent.features.mail_ops.reader_service.mail_fetch_cron", fake_mail_fetch)
        handler_entry = cmd_reg.get("/mail_fetch")
        assert handler_entry is not None
        handler_entry["handler"](mock_agent, mock_msg, [])
        assert recorded_agent == [mock_agent], "mail_fetch 必须向 reader 注入宿主 agent"



def test_channels_has_no_features_or_skills_reverse_dependency():
    """Item 2: channels/api.py 不再导入任何 features 或 skills。"""
    api_path = PACKAGE_ROOT / "channels" / "api.py"
    assert api_path.exists()
    content = api_path.read_text(encoding="utf-8")
    tree = ast.parse(content, filename=str(api_path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                assert root_pkg not in ("features", "skills", "scripts"), (
                    f"channels/api.py 严禁导入业务模块: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_pkg = node.module.split(".")[0]
                assert root_pkg not in ("features", "skills", "scripts"), (
                    f"channels/api.py 严禁导入业务模块: {node.module}"
                )


def test_kernel_stage2_billing_debt_fully_cleared():
    """Item 3: core/ 历史兼容文件已删除，architecture_rules 中 0 条 kernel_stage_2 依赖。"""
    assert not (PACKAGE_ROOT / "core" / "billing_workflow.py").exists(), "core/billing_workflow.py 必须已物理删除"
    assert not (PACKAGE_ROOT / "core" / "billing_snapshot.py").exists(), "core/billing_snapshot.py 必须已物理删除"

    rules_file = ROOT / "dev/config/architecture/rules.json"
    if not rules_file.exists():
        rules_file = ROOT / "config/architecture/rules.json"
    rules_data = json.loads(rules_file.read_text(encoding="utf-8"))
    stage2_items = [
        item for item in rules_data.get("exceptions", [])
        if item.get("removal_stage") == "kernel_stage_2"
    ]
    assert len(stage2_items) == 0, f"architecture_rules.json 仍残留 {len(stage2_items)} 项 kernel_stage_2 债务"

    # 验证 skills/ops_billing.py 内部调用已切换为 canonical 实现
    ops_billing_code = (PACKAGE_ROOT / "skills" / "ops_billing.py").read_text(encoding="utf-8")
    assert "lite_agent.core.billing_snapshot" not in ops_billing_code, "skills/ops_billing.py 内部调用仍导入 core.billing_snapshot"
    assert "lite_agent.features.billing.snapshot" in ops_billing_code, "skills/ops_billing.py 必须使用 canonical features.billing.snapshot"


def test_review_meeting_room_and_thin_cli():
    """Item 4.1: 会议业务主体归位 features/review_meeting/room.py，scripts/review_meeting.py 退化为薄 CLI。"""
    room_py = PACKAGE_ROOT / "features" / "review_meeting" / "room.py"
    cli_py = ROOT / "scripts" / "review_meeting.py"
    assert room_py.exists(), "features/review_meeting/room.py 必须存在"
    assert cli_py.exists(), "scripts/review_meeting.py 必须存在"

    cli_lines = [line.strip() for line in cli_py.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]
    assert len(cli_lines) <= 15, f"scripts/review_meeting.py 必须是薄 CLI (当前非空行: {len(cli_lines)})"

    # features/review_meeting 内部模块严禁反向依赖 scripts
    for mod_name in ("api.py", "http_adapter.py", "notifications.py", "channel_commands.py"):
        mod_code = (PACKAGE_ROOT / "features" / "review_meeting" / mod_name).read_text(encoding="utf-8")
        assert "from scripts import review_meeting" not in mod_code, f"{mod_name} 仍反向导入 scripts.review_meeting"
        assert "from lite_agent.features.review_meeting import room" in mod_code or "from features.review_meeting import room" in mod_code, f"{mod_name} 必须使用 canonical room"


def test_request_selector_domain_map_decoupled_from_core():
    """Item 4.2: core/request_selector.py 源码中不硬编码业务领域，业务路由由插件提供。"""
    selector_py = PACKAGE_ROOT / "core" / "request_selector.py"
    code = selector_py.read_text(encoding="utf-8")

    # 核心源码中不得直接硬编码业务特定 pattern
    assert "mail_fetch_summaries" not in code, "core/request_selector.py 不得硬编码 mail 工具"
    assert "billing_due_soon" not in code, "core/request_selector.py 不得硬编码 billing 工具"
    assert "todo_push_brief" not in code, "core/request_selector.py 不得硬编码 todo 工具"

    # core 模式纯内核启动时不应包含业务领域
    from lite_agent.core.request_selector import CORE_DOMAIN_MAP, RequestSelector
    assert "system" in CORE_DOMAIN_MAP
    assert "llm" in CORE_DOMAIN_MAP
    assert "mail" not in CORE_DOMAIN_MAP
    assert "billing" not in CORE_DOMAIN_MAP
    assert "todo" not in CORE_DOMAIN_MAP

    # RequestSelector 支持动态注册与注入
    dummy_engine = SimpleNamespace(get_all_names=lambda: {"custom_tool", "ops_sys_status"})
    selector = RequestSelector(dummy_engine, domain_map={})
    assert selector.select("测试未知文本").names is None

    selector.register_domain("custom", {
        "pattern": r"(自定义|custom)",
        "default_tools": ["custom_tool"],
        "explicit_intent_tools": [],
        "explicit_patterns": None,
    })
    res = selector.select("这是一个自定义请求")
    assert res.names == ["custom_tool"]
    assert "custom" in res.domains
