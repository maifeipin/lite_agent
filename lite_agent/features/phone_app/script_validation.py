"""
受限设备脚本 (JSON DSL) 静态安全与语法校验器 (features.phone_app.script_validation).
严格遵循第 7 节、第 8 节与第 13 节：
- 严格白名单动作：open_app, tap, fill, submit, wait_for, extract, screenshot, back
- 严格参数长度、步数、等待时间限额与注入检测。
- 拒绝未知字段，禁止任何 Bash、Shell 插值、eval/exec 或网络/本地文件调用。
"""

from __future__ import annotations
import re
from typing import Dict, Any, List, Tuple, Set

ALLOWED_ACTIONS: Set[str] = {
    "open_app",
    "tap",
    "fill",
    "submit",
    "wait_for",
    "extract",
    "screenshot",
    "back",
}

ACTION_ALLOWED_KEYS: Dict[str, Set[str]] = {
    "open_app": {"action", "package"},
    "tap": {"action", "target"},
    "fill": {"action", "target", "text"},
    "submit": {"action", "target"},
    "wait_for": {"action", "target", "timeout"},
    "extract": {"action", "fields"},
    "screenshot": {"action"},  # 产物路径由执行层统一分配，严禁脚本指定外部路径 (B07)
    "back": {"action"},
}

MAX_STEPS = 25
MAX_SELECTOR_LEN = 120
MAX_TEXT_LEN = 100
MAX_WAIT_SECONDS = 15

# 禁止包含命令注入特性的敏感字符与控制符
SHELL_INJECTION_PATTERN = re.compile(r"[`$;|&><\n\r\t]|\$\(.*\)")


class ScriptValidationError(Exception):
    pass


def validate_script_dsl(
    script: List[Dict[str, Any]],
    allowed_packages: Set[str]
) -> Tuple[bool, Optional[str]]:
    """验证单个 App 的 DSL 步骤列表是否完全符合安全规范 (B07: 严格逐动作字段白名单)。"""
    if not isinstance(script, list):
        return False, "脚本必须为 JSON 步骤列表 (Array of Steps)"

    if len(script) == 0:
        return False, "脚本步骤不能为空"

    if len(script) > MAX_STEPS:
        return False, f"脚本步数超出安全上限 ({len(script)} > {MAX_STEPS})"

    for idx, step in enumerate(script, 1):
        if not isinstance(step, dict):
            return False, f"步骤 #{idx} 必须为 JSON 对象"

        action = step.get("action")
        if not action or not isinstance(action, str):
            return False, f"步骤 #{idx} 缺少有效的 'action' 字段"

        if action not in ALLOWED_ACTIONS:
            return False, f"步骤 #{idx} 包含未允许的动作: '{action}'"

        # 1. 严格动作级参数白名单检查 (B07: 拒绝任何额外未知字段)
        allowed_keys = ACTION_ALLOWED_KEYS.get(action, set())
        extra_keys = set(step.keys()) - allowed_keys
        if extra_keys:
            return False, f"步骤 #{idx} ({action}) 包含未白名单的非法字段: {list(extra_keys)}"

        # 2. 校验动作专有参数
        if action == "open_app":
            pkg = step.get("package")
            if not pkg or not isinstance(pkg, str):
                return False, f"步骤 #{idx} (open_app) 缺少 'package' 字段"
            if pkg not in allowed_packages:
                return False, f"步骤 #{idx} (open_app) 包名 '{pkg}' 不在当前受信任 App 清单中"

        elif action == "tap":
            target = step.get("target")
            if not target or not isinstance(target, (str, list)):
                return False, f"步骤 #{idx} (tap) 缺少有效的 'target' 目标选择器或坐标"
            if isinstance(target, str):
                if len(target) > MAX_SELECTOR_LEN:
                    return False, f"步骤 #{idx} 选择器过长 (> {MAX_SELECTOR_LEN})"
                if SHELL_INJECTION_PATTERN.search(target):
                    return False, f"步骤 #{idx} 选择器包含非法或潜在注入字符"
            elif isinstance(target, list):
                if len(target) != 2 or not all(isinstance(v, int) for v in target):
                    return False, f"步骤 #{idx} 坐标必须为形如 [X, Y] 的整数列表"

        elif action == "fill":
            target = step.get("target")
            text = step.get("text")
            if not target or not isinstance(target, str):
                return False, f"步骤 #{idx} (fill) 缺少 'target' 选择器"
            if text is None or not isinstance(text, str):
                return False, f"步骤 #{idx} (fill) 缺少 'text' 文本内容"
            if len(target) > MAX_SELECTOR_LEN:
                return False, f"步骤 #{idx} (fill) 选择器过长"
            if len(text) > MAX_TEXT_LEN:
                return False, f"步骤 #{idx} (fill) 输入文本超出长度限制 (> {MAX_TEXT_LEN})"
            if SHELL_INJECTION_PATTERN.search(target):
                return False, f"步骤 #{idx} (fill) 选择器包含非法字符"
            if re.search(r"[`$\n\r]|\$\(.*\)", text):
                return False, f"步骤 #{idx} (fill) 输入文本包含非法控制字符"

        elif action == "wait_for":
            target = step.get("target")
            timeout = step.get("timeout", 5)
            if not target or not isinstance(target, str):
                return False, f"步骤 #{idx} (wait_for) 缺少 'target' 预期文本或选择器"
            if not isinstance(timeout, (int, float)) or timeout > MAX_WAIT_SECONDS or timeout < 0.1:
                return False, f"步骤 #{idx} (wait_for) 等待超时必须在 0.1 ~ {MAX_WAIT_SECONDS} 秒之间"

        elif action == "extract":
            fields = step.get("fields")
            if not isinstance(fields, list) or len(fields) == 0:
                return False, f"步骤 #{idx} (extract) 必须指定非空的 'fields' 字段列表"
            allowed_fields = {"title", "price", "amount", "shop", "shop_name", "sales", "positive_rate", "free_shipping"}
            extra_fields = set(fields) - allowed_fields
            if extra_fields:
                return False, f"步骤 #{idx} (extract) 包含未支持的提取字段: {list(extra_fields)}"

    return True, None
