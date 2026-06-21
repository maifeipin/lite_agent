"""
动态技能引擎 - 自动扫描 skills/ 目录，注册技能并生成 OpenAI Tool Schema
"""

import os
import sys
import json
import time
import importlib
import traceback
from typing import Any, Dict, List, Optional

from core.constants import PROJECT_ROOT
AUDIT_LOG = os.path.join(PROJECT_ROOT, 'workspace', 'audit.log')

# 工具结果最大长度（字符）。超过则保留头尾、丢弃中段，避免单个工具返回撑爆 LLM 上下文
# 12000 字符 ≈ 中文 ~4000 token / 英文 ~3000 token，留足后续轮次空间
MAX_TOOL_RESULT_LEN = 12000


def _cap_tool_result(skill_name: str, result: str, max_len: int = MAX_TOOL_RESULT_LEN) -> str:
    """超长工具结果做头尾截断，并在中段插入提示，引导模型用更精确的参数重新调用"""
    if len(result) <= max_len:
        return result
    keep_head = max_len // 3
    keep_tail = max_len - keep_head - 200  # 留 200 字符给提示
    omitted = len(result) - keep_head - keep_tail
    notice = (
        f"\n\n... ⚠️ [已截断] 原始结果共 {len(result)} 字符，超过单次返回上限 "
        f"{max_len}，中间省略 {omitted} 字符。"
        f"请考虑更精确的参数（如缩小时间范围、增加关键词）重新调用。\n\n..."
    )
    return result[:keep_head] + notice + result[-keep_tail:]


# ============================================================
#  全局技能注册表
# ============================================================
_skill_registry: Dict[str, Dict] = {}  # {name: {"func": callable, "schema": dict}}


def skill(name: str, description: str, params: dict = None, tags: list = None, guest_ok: bool = False, guard_keywords: list = None, guard_prompt: str = "", guard_threshold: int = 1):
    """
    技能装饰器 - 标记一个函数为可被 AI 调用的技能

    用法:
        @skill(
            name="ops_sys_status",
            description="获取 VPS 系统状态",
            params={
                "detail": {
                    "type": "boolean",
                    "description": "是否返回详细信息",
                    "default": False
                }
            },
            tags=["sys", "text"],
            guest_ok=False,
            guard_keywords=["系统", "status"],
            guard_prompt="请勿捏造系统状态...",
            guard_threshold=1
        )
        def ops_sys_status(detail: bool = False) -> str:
            ...
    """
    def decorator(func):
        # 构建 OpenAI Function Calling 的参数 Schema
        properties = {}
        required = []

        if params:
            for param_name, param_info in params.items():
                prop = {
                    "type": param_info.get("type", "string"),
                    "description": param_info.get("description", ""),
                }
                if "enum" in param_info:
                    prop["enum"] = param_info["enum"]
                properties[param_name] = prop
                # 没有 default 的参数视为必填
                if "default" not in param_info:
                    required.append(param_name)

        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

        _skill_registry[name] = {
            "func": func,
            "schema": schema,
            "tags": tags or [],
            "guest_ok": guest_ok,
            "guard_keywords": guard_keywords or [],
            "guard_prompt": guard_prompt,
            "guard_threshold": guard_threshold,
        }
        func._skill_name = name
        return func

    return decorator


# ============================================================
#  技能引擎
# ============================================================
class SkillEngine:
    """
    技能引擎
    - 启动时自动扫描 skills/ 目录
    - 将 Python 函数自动转换为 OpenAI Tool Schema
    - 提供统一的执行调度接口
    """

    def __init__(self, skills_dir: str = None):
        if skills_dir is None:
            from core.constants import PROJECT_ROOT
            skills_dir = os.path.join(PROJECT_ROOT, "skills")
        self.skills_dir = skills_dir
        self._load_skills()

    def _load_skills(self):
        """扫描 skills/ 目录，自动导入所有技能模块"""
        if not os.path.isdir(self.skills_dir):
            print(f"⚠️ 技能目录不存在: {self.skills_dir}")
            return

        # 确保 skills 目录及其父目录在 sys.path 中
        parent_dir = os.path.dirname(self.skills_dir)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        if self.skills_dir not in sys.path:
            sys.path.insert(0, self.skills_dir)

        for filename in sorted(os.listdir(self.skills_dir)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            module_name = filename[:-3]
            try:
                # 使用 skills.xxx 的方式导入
                full_module = f"skills.{module_name}"
                if full_module in sys.modules:
                    importlib.reload(sys.modules[full_module])
                else:
                    importlib.import_module(full_module)
                print(f"  ✅ 已加载技能模块: {filename}")
            except Exception as e:
                print(f"  ❌ 加载技能模块失败 [{filename}]: {e}")
                traceback.print_exc()

        print(f"📦 技能引擎就绪: 共注册 {len(_skill_registry)} 个技能")

    def get_all_schemas(self) -> List[Dict]:
        """返回所有已注册技能的 OpenAI Tool Schema 列表"""
        return [info["schema"] for info in _skill_registry.values()]

    def get_guest_schemas(self) -> List[Dict]:
        """返回所有已注册且 guest_ok=True 的技能 OpenAI Tool Schema 列表"""
        return [info["schema"] for info in _skill_registry.values() if info.get("guest_ok")]

    def get_schemas_by_names(self, names: list) -> List[Dict]:
        """返回指定名称的技能 Schema"""
        if not names:
            return self.get_all_schemas()
        return [info["schema"] for name, info in _skill_registry.items()
                if name in names]

    def get_schemas_by_tag(self, tag: str) -> List[Dict]:
        """按标签筛选技能 Schema"""
        return [info["schema"] for info in _skill_registry.values()
                if tag in (info.get("tags") or [])]

    def get_gemini_tool_declarations(self, names: list = None) -> list:
        """将 OpenAI tool schema 转为 Gemini function_declarations 格式"""
        decls = []
        items = (
            [(n, _skill_registry[n]) for n in names if n in _skill_registry]
            if names
            else _skill_registry.items()
        )
        for name, info in items:
            fn = info["schema"]["function"]
            params = fn["parameters"]
            gemini_params = {
                "type": params.get("type", "OBJECT").upper(),
                "properties": {},
                "required": params.get("required", []),
            }
            for pname, pdef in params.get("properties", {}).items():
                gemini_params["properties"][pname] = {
                    "type": pdef.get("type", "STRING").upper(),
                    "description": pdef.get("description", ""),
                }
                if "enum" in pdef:
                    gemini_params["properties"][pname]["enum"] = pdef["enum"]
            decls.append({
                "name": fn["name"],
                "description": fn["description"],
                "parameters": gemini_params,
            })
        return decls

    def execute(self, skill_name: str, arguments: str) -> str:
        """
        执行指定技能
        :param skill_name: 技能名称
        :param arguments: JSON 格式的参数字符串
        :return: 执行结果字符串
        """
        if skill_name not in _skill_registry:
            return f"❌ 未知技能: {skill_name}"

        func = _skill_registry[skill_name]["func"]

        # 解析参数
        try:
            kwargs = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as e:
            return f"❌ 参数解析失败: {e}"

        # 执行
        try:
            print(f"  🔧 执行技能: {skill_name}({kwargs})")
            _write_audit(skill_name, arguments)
            result = func(**kwargs)
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, indent=2)
            raw_len = len(result)
            result = _cap_tool_result(skill_name, result)
            if len(result) != raw_len:
                print(f"  ✂️ 工具结果截断: {skill_name} {raw_len} → {len(result)} 字符")
            else:
                print(f"  ✓ {skill_name} result_len={raw_len}")
            return result
        except Exception as e:
            error_msg = f"❌ 技能执行异常 [{skill_name}]: {e}"
            print(error_msg)
            traceback.print_exc()
            return error_msg

    def list_skills(self, is_guest: bool = False) -> str:
        """列出所有已注册技能 (供 System Prompt 和 /help 展示)"""
        if not _skill_registry:
            return "(暂无技能)"
        lines = []
        for name, info in _skill_registry.items():
            if is_guest and not info.get("guest_ok"):
                continue
            desc = info["schema"]["function"]["description"]
            params = info["schema"]["function"]["parameters"]["properties"]
            param_str = ", ".join(params.keys()) if params else "无参数"
            lines.append(f"- **{name}**({param_str}): {desc}")
        return "\n".join(lines) if lines else "(无可用工具)"

    def list_skills_filtered(self, names: list) -> str:
        """列出指定名称的技能"""
        if not _skill_registry:
            return "(暂无技能)"
        lines = []
        name_set = set(names) if names else set()
        for name, info in _skill_registry.items():
            if name_set and name not in name_set:
                continue
            desc = info["schema"]["function"]["description"]
            params = info["schema"]["function"]["parameters"]["properties"]
            param_str = ", ".join(params.keys()) if params else "无参数"
            lines.append(f"- **{name}**({param_str}): {desc}")
        return "\n".join(lines) if lines else "(无可用工具)"

    def get_skill_count(self) -> int:
        """返回已注册技能数量"""
        return len(_skill_registry)

    def is_guest_ok(self, skill_name: str) -> bool:
        """检查指定技能是否允许访客调用"""
        info = _skill_registry.get(skill_name)
        if not info:
            return False
        return bool(info.get("guest_ok"))

    def get_guard_prompts(self, text: str, is_guest: bool = False) -> List[str]:
        """检查消息是否命中技能的 guard_keywords，返回对应 guard_prompt"""
        prompts = []
        for name, info in _skill_registry.items():
            if is_guest and not info.get("guest_ok"):
                continue
            kws = info.get("guard_keywords", [])
            prompt = info.get("guard_prompt")
            threshold = info.get("guard_threshold", 1)
            if kws and prompt:
                matched_count = sum(1 for kw in kws if kw in text)
                if matched_count >= threshold:
                    if prompt not in prompts:
                        prompts.append(prompt)
        return prompts


def _write_audit(name: str, args: str):
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        args_short = args[:200] if len(args) > 200 else args
        with open(AUDIT_LOG, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] {name}  {args_short}\n')
    except Exception:
        pass
