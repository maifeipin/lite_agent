#!/usr/bin/env python3
"""
静态架构依赖门禁检查器 (check_architecture.py)
基于 Python 标准库 AST，检查仓库内代码是否违反架构分层依赖约束。

规则包括：
1. RULE_NO_DIRECT_PARSER_IMPORT: 禁止 Agent 生产代码直接导入 parser 模块 (如 mail_client, statement_parser 等)
2. RULE_NO_CORE_REVERSE_DEPENDENCY / RULE_NO_KERNEL_BUSINESS_DEPENDENCY:
   禁止基础内核 (core/, services/, contracts/, app/, agent.py, session.py, security.py) 产生对 features/、skills/、scripts/ 或具体业务工作流的反向依赖
3. RULE_NO_CONTRACTS_DEPENDENCY: contracts 契约层严禁依赖任何外部运行时或业务模块，仅限标准库
4. RULE_NO_SERVICES_DEPENDENCY: services 服务层严禁反向依赖完整 agent 运行时
5. RULE_DYNAMIC_IMPORT_REGISTERED: 禁止通过未登记的动态调用 (importlib.import_module / __import__ 及别名) 绕过门禁
6. RULE_NO_CROSS_REPO_SYS_PATH: 禁止在生产代码中动态注入跨仓外部路径到 sys.path
7. RULE_NO_DIRECT_SQLITE_IN_BILLING: 禁止 features/billing/ 业务代码直接导入 sqlite3 直连解析/账单数据库
8. RULE_NO_FEATURE_SYS_MODULES_LOOKUP: 禁止 features/ 特性业务代码通过 sys.modules 反查上层技能或兼容层模块

使用方式：
    python scripts/check_architecture.py
    python scripts/check_architecture.py --config architecture_rules.json
"""

import ast
import json
import os
import subprocess
import sys
from typing import Dict, List, Set, Tuple, Optional, Any, Union

PARSER_MODULES = {
    "mail_client",
    "statement_parser",
    "statement_db",
    "statement_models",
    "statement_evidence",
    "mail_quality",
    "billing_readmodel",
    "mail_sync",
    "mail_connect",
    "mail_parse",
    "mail_llm",
}


def load_rules_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"找不到架构规则配置文件: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_repo_python_files(repo_root: str, target_dir: Optional[str] = None) -> List[str]:
    """获取待扫描的 Python 文件列表（通过 git ls-files -z 全面枚举跟踪与未跟踪工作区文件）。"""
    if target_dir:
        abs_target = os.path.abspath(target_dir)
        py_files = []
        for root, _, files in os.walk(abs_target):
            for f in files:
                if f.endswith(".py"):
                    py_files.append(os.path.relpath(os.path.join(root, f), repo_root))
        return sorted(py_files)

    files_set = set()
    try:
        res = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
        raw_items = [item for item in res.stdout.split(b"\0") if item]
        for item in raw_items:
            path_str = item.decode("utf-8", errors="replace")
            if path_str.endswith(".py"):
                files_set.add(path_str)
    except Exception:
        pass

    if not files_set:
        for root, _, files in os.walk(repo_root):
            for f in files:
                if f.endswith(".py"):
                    files_set.add(os.path.relpath(os.path.join(root, f), repo_root))

    excluded_prefixes = (".git", ".venv", "venv", "scratch", "docs", "architecture", ".local", ".gemini", "deploy")
    result = []
    for f in sorted(files_set):
        norm = os.path.normpath(f).replace("\\", "/")
        if any(norm.startswith(p) for p in excluded_prefixes):
            continue
        if os.path.isfile(os.path.join(repo_root, f)):
            result.append(norm)
    return result


def is_local_repo_root_expr(node: ast.AST) -> bool:
    """识别本仓库脚本添加自身根目录到 sys.path 的标准样板代码，不误报为跨仓注入。"""
    if isinstance(node, ast.Constant) and node.value in (".", ""):
        return True
    if isinstance(node, ast.Name) and node.id in ("PROJECT_ROOT", "project_root", "__BASE_DIR__"):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "dirname":
            return True
    return False


CONTRACT_STDLIB = frozenset({
    "__future__", "abc", "collections", "copy", "dataclasses", "datetime",
    "decimal", "enum", "functools", "itertools", "json", "math", "numbers",
    "operator", "re", "types", "typing", "uuid",
})


class ArchitectureVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str):
        norm = os.path.normpath(rel_path).replace("\\", "/")
        self.rel_path = norm

        # 分层判定
        self.is_contracts = norm.startswith("contracts/") or norm == "contracts.py" or norm.startswith("lite_agent/contracts/")
        self.is_services = norm.startswith("services/") or norm == "services.py" or norm.startswith("lite_agent/services/")
        self.is_app = norm.startswith("app/") or norm == "app.py" or norm.startswith("lite_agent/app/")
        self.is_core = norm.startswith("core/") or norm == "core.py" or norm.startswith("lite_agent/core/")
        self.is_channels = norm.startswith("channels/") or norm == "channels.py" or norm.startswith("lite_agent/channels/")
        self.is_kernel_root = norm in (
            "main.py", "agent.py", "session.py", "security.py",
            "lite_agent/cli.py", "lite_agent/agent.py", "lite_agent/session.py", "lite_agent/security.py"
        )
        self.is_kernel = self.is_contracts or self.is_services or self.is_app or self.is_core or self.is_kernel_root

        self.is_test = norm.startswith("tests/") or norm.startswith("dev/tests/")
        self.is_billing_feature = norm.startswith("features/billing") or norm.startswith("lite_agent/features/billing")
        self.is_feature = norm.startswith("features/") or norm.startswith("lite_agent/features/")

        self.findings: List[Dict[str, Any]] = []
        self.scope_stack: List[Union[ast.FunctionDef, ast.AsyncFunctionDef]] = []

        # 跟踪动态导入函数的局部别名
        self.importlib_aliases: Set[str] = {"importlib"}
        self.import_module_aliases: Set[str] = {"import_module"}
        self.dunder_import_aliases: Set[str] = {"__import__"}
        self.builtins_aliases: Set[str] = {"builtins"}

    def _record_finding(self, rule: str, target: str, line: int, detail: str, suggestion: str):
        scope_name = self.scope_stack[-1].name if self.scope_stack else "<module>"
        scope_args = [arg.arg for arg in self.scope_stack[-1].args.args] if self.scope_stack else []
        self.findings.append({
            "rule": rule,
            "target": target,
            "line": str(line),
            "detail": detail,
            "suggestion": suggestion,
            "scope": scope_name,
            "scope_args": scope_args,
        })

    def _resolve_relative_module(self, module: Optional[str], level: int) -> str:
        """根据 relative level 解析规范化模块全名。"""
        if level == 0:
            return module or ""
        parts = self.rel_path.split("/")[:-1]
        if level <= len(parts):
            base = parts[:len(parts) - level + 1]
        else:
            base = []
        if module:
            return ".".join(base + [module]) if base else module
        return ".".join(base)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.scope_stack.append(node)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self.scope_stack.append(node)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            mod_name = alias.name
            asname = alias.asname or alias.name
            root_mod = mod_name.split(".")[0]

            if root_mod == "importlib":
                self.importlib_aliases.add(asname)
            if root_mod == "builtins":
                self.builtins_aliases.add(asname)

            # 规则 1: 禁止直接导入 parser 模块
            if root_mod in PARSER_MODULES:
                self._record_finding(
                    "RULE_NO_DIRECT_PARSER_IMPORT",
                    root_mod,
                    node.lineno,
                    f"import {mod_name}",
                    "改用 MailCLI 或 integrations 适配器进行跨仓调用",
                )

            # 规则 2: 基础内核代码与公共 channels 严禁导入业务特性、技能与脚本
            is_business_import = root_mod in ("features", "skills", "scripts") or (
                root_mod == "lite_agent" and len(mod_name.split(".")) > 1 and mod_name.split(".")[1] in ("features", "skills", "scripts")
            )
            if (self.is_kernel or self.is_channels) and is_business_import:
                rule_name = "RULE_NO_CORE_REVERSE_DEPENDENCY" if self.is_core else "RULE_NO_KERNEL_BUSINESS_DEPENDENCY"
                self._record_finding(
                    rule_name,
                    mod_name,
                    node.lineno,
                    f"公共通道或内核代码导入业务模块: {mod_name}",
                    "将业务逻辑解耦迁出至 features/ 或通过端口注入",
                )

            # 规则 3: contracts 严禁依赖任何外部运行时
            is_contracts_allowed = root_mod in CONTRACT_STDLIB | {"contracts"} or mod_name.startswith("lite_agent.contracts")
            if self.is_contracts and not is_contracts_allowed:
                self._record_finding(
                    "RULE_NO_CONTRACTS_DEPENDENCY",
                    mod_name,
                    node.lineno,
                    f"contracts 契约层依赖外部运行时模块: {mod_name}",
                    "contracts 必须保持纯净，仅依赖 Python 标准库",
                )

            # 规则 4: services 严禁反向依赖完整 agent 运行时
            is_agent_import = root_mod == "agent" or mod_name == "lite_agent.agent" or mod_name.startswith("lite_agent.agent.")
            if self.is_services and is_agent_import:
                self._record_finding(
                    "RULE_NO_SERVICES_DEPENDENCY",
                    mod_name,
                    node.lineno,
                    f"services 服务层反向依赖完整 agent 运行时: {mod_name}",
                    "services 必须通过 contracts 消息类型与参数传递解耦，禁止直接导入 agent",
                )

            # 规则 5: features/billing 禁止直接导入 sqlite3
            if self.is_billing_feature and root_mod == "sqlite3":
                self._record_finding(
                    "RULE_NO_DIRECT_SQLITE_IN_BILLING",
                    "sqlite3",
                    node.lineno,
                    "features/billing 导入直接数据库模块 sqlite3",
                    "features/billing 必须通过 MailCLI 调用外部 Parser 读模型，禁止直接连接数据库",
                )

        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        resolved_mod = self._resolve_relative_module(node.module, node.level)
        root_mod = resolved_mod.split(".")[0]

        if resolved_mod == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    self.import_module_aliases.add(alias.asname or alias.name)
        elif resolved_mod == "builtins":
            for alias in node.names:
                if alias.name == "__import__":
                    self.dunder_import_aliases.add(alias.asname or alias.name)

        # 规则 1: 禁止直接 from parser_mod import ...
        if root_mod in PARSER_MODULES:
            self._record_finding(
                "RULE_NO_DIRECT_PARSER_IMPORT",
                root_mod,
                node.lineno,
                f"from {resolved_mod} import ...",
                "改用 MailCLI 或 integrations 适配器进行跨仓调用",
            )

        # 规则 2: 基础内核代码与公共 channels 严禁导入业务特性、技能与脚本 (支持绝对与相对导入解析)
        if self.is_kernel or self.is_channels:
            for alias in node.names:
                full_mod = f"{resolved_mod}.{alias.name}" if resolved_mod else alias.name
                root_item = full_mod.split(".")[0]

                is_business_item = root_item in ("features", "skills", "scripts") or (
                    root_item == "lite_agent" and len(full_mod.split(".")) > 1 and full_mod.split(".")[1] in ("features", "skills", "scripts")
                )
                if is_business_item:
                    rule_name = "RULE_NO_CORE_REVERSE_DEPENDENCY" if self.is_core else "RULE_NO_KERNEL_BUSINESS_DEPENDENCY"
                    self._record_finding(
                        rule_name,
                        full_mod,
                        node.lineno,
                        f"公共通道或内核代码导入业务模块: {full_mod}",
                        "将业务逻辑解耦迁出至 features/ 或通过插件机制装配",
                    )

        # 规则 3: contracts 严禁依赖任何外部运行时
        if self.is_contracts:
            for alias in node.names:
                full_mod = f"{resolved_mod}.{alias.name}" if resolved_mod else alias.name
                root_item = full_mod.split(".")[0]
                is_contracts_item_allowed = root_item in CONTRACT_STDLIB | {"contracts"} or full_mod.startswith("lite_agent.contracts")
                if not is_contracts_item_allowed:
                    self._record_finding(
                        "RULE_NO_CONTRACTS_DEPENDENCY",
                        full_mod,
                        node.lineno,
                        f"contracts 契约层依赖外部运行时模块: {full_mod}",
                        "contracts 必须保持纯净，仅依赖 Python 标准库",
                    )

        # 规则 4: services 严禁反向依赖完整 agent 运行时
        if self.is_services:
            for alias in node.names:
                full_mod = f"{resolved_mod}.{alias.name}" if resolved_mod else alias.name
                root_item = full_mod.split(".")[0]
                is_agent_item = root_item == "agent" or full_mod == "lite_agent.agent" or full_mod.startswith("lite_agent.agent.")
                if is_agent_item:
                    self._record_finding(
                        "RULE_NO_SERVICES_DEPENDENCY",
                        full_mod,
                        node.lineno,
                        f"services 服务层反向依赖完整 agent 运行时: {full_mod}",
                        "services 必须通过 contracts 消息类型与参数传递解耦，禁止直接导入 agent",
                    )

        # 规则 5: features/billing 禁止从 sqlite3 导入
        if self.is_billing_feature and root_mod == "sqlite3":
            self._record_finding(
                "RULE_NO_DIRECT_SQLITE_IN_BILLING",
                "sqlite3",
                node.lineno,
                f"from {resolved_mod} import ...",
                "features/billing 必须通过 MailCLI 调用外部 Parser 读模型，禁止直接连接数据库",
            )

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript):
        if self.is_feature:
            if isinstance(node.value, ast.Attribute) and node.value.attr == "modules":
                if isinstance(node.value.value, ast.Name) and node.value.value.id == "sys":
                    target_str = ""
                    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                        target_str = node.slice.value
                    forbidden_sys_modules = (
                        "skills.", "integrations.", "core.",
                        "lite_agent.skills.", "lite_agent.integrations.", "lite_agent.core."
                    )
                    if target_str and any(target_str.startswith(p) for p in forbidden_sys_modules):
                        self._record_finding(
                            "RULE_NO_FEATURE_SYS_MODULES_LOOKUP",
                            target_str,
                            node.lineno,
                            f"features 模块通过 sys.modules 反向查找旧入口: {target_str}",
                            "严禁 features 反向查找旧入口模块，依赖必须通过参数显式注入",
                        )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare):
        if self.is_feature:
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    if isinstance(comparator, ast.Attribute) and comparator.attr == "modules":
                        if isinstance(comparator.value, ast.Name) and comparator.value.id == "sys":
                            if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                                target_str = node.left.value
                                forbidden_sys_modules = (
                                    "skills.", "integrations.", "core.",
                                    "lite_agent.skills.", "lite_agent.integrations.", "lite_agent.core."
                                )
                                if target_str and any(target_str.startswith(p) for p in forbidden_sys_modules):
                                    self._record_finding(
                                        "RULE_NO_FEATURE_SYS_MODULES_LOOKUP",
                                        target_str,
                                        node.lineno,
                                        f"features 模块通过 sys.modules 检查旧入口: {target_str}",
                                        "严禁 features 反向查找旧入口模块，依赖必须通过参数显式注入",
                                    )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        value = node.value
        imported = (isinstance(value, ast.Name) and value.id in self.import_module_aliases)
        builtin = (isinstance(value, ast.Name) and value.id in self.dunder_import_aliases)
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            imported = value.attr == "import_module" and value.value.id in self.importlib_aliases
            builtin = value.attr == "__import__" and value.value.id in self.builtins_aliases
        for target in node.targets:
            if isinstance(target, ast.Name):
                if imported:
                    self.import_module_aliases.add(target.id)
                if builtin:
                    self.dunder_import_aliases.add(target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        func = node.func
        is_dynamic = False

        # 检查是否调用动态导入（支持别名跟踪）
        if isinstance(func, ast.Name):
            if func.id in self.import_module_aliases or func.id in self.dunder_import_aliases:
                is_dynamic = True
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            is_dynamic = (
                (func.attr == "import_module" and func.value.id in self.importlib_aliases)
                or (func.attr == "__import__" and func.value.id in self.builtins_aliases)
            )

        if is_dynamic:
            target_name = ""
            keyword_name = next((kw.value for kw in node.keywords if kw.arg == "name"), None)
            if node.args or keyword_name is not None:
                first_arg = node.args[0] if node.args else keyword_name
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    target_name = first_arg.value
                else:
                    target_name = "<dynamic_expression>"
            else:
                target_name = "<no_args>"

            if target_name == "<dynamic_expression>":
                self._record_finding(
                    "RULE_DYNAMIC_IMPORT_REGISTERED",
                    "<dynamic_expression>",
                    node.lineno,
                    "未登记的表达式动态导入",
                    "动态导入未知变量/表达式必须在 architecture_rules.json 中显式登记",
                )
            else:
                root_mod = target_name.split(".")[0]
                if (root_mod in PARSER_MODULES
                        or ((self.is_kernel or self.is_channels) and root_mod in ("features", "skills", "scripts"))
                        or (self.is_contracts and root_mod not in CONTRACT_STDLIB | {"contracts"})
                        or (self.is_services and root_mod == "agent")):
                    self._record_finding(
                        "RULE_DYNAMIC_IMPORT_REGISTERED",
                        target_name,
                        node.lineno,
                        f"动态导入敏感模块: {target_name}",
                        "动态导入跨仓/敏感模块必须在 architecture_rules.json 中显式登记精确例外",
                    )

        # 检查生产代码与测试代码中的 sys.path 跨仓动态修改
        is_sys_path_mod = False
        target_arg = None
        if isinstance(func, ast.Attribute) and func.attr in ("insert", "append", "extend"):
            if isinstance(func.value, ast.Attribute) and func.value.attr == "path":
                if isinstance(func.value.value, ast.Name) and func.value.value.id == "sys":
                    is_sys_path_mod = True
                    target_arg = node.args[-1] if node.args else None
        elif isinstance(func, ast.Attribute) and func.attr == "syspath_prepend":
            is_sys_path_mod = True
            target_arg = node.args[0] if node.args else None

        if is_sys_path_mod and target_arg and not is_local_repo_root_expr(target_arg):
            arg_name = "path_arg"
            if isinstance(target_arg, ast.Name):
                arg_name = target_arg.id
            elif isinstance(target_arg, ast.Attribute):
                arg_name = target_arg.attr
            elif isinstance(target_arg, ast.Call):
                if isinstance(target_arg.func, ast.Name) and target_arg.func.id == "str" and target_arg.args:
                    if isinstance(target_arg.args[0], ast.Name):
                        arg_name = target_arg.args[0].id
            self._record_finding(
                "RULE_NO_CROSS_REPO_SYS_PATH",
                arg_name,
                node.lineno,
                f"操作 sys.path 外部路径: {arg_name}",
                "禁止未登记的跨仓 sys.path 注入",
            )

        self.generic_visit(node)


def match_exception(finding: dict, rel_path: str, exceptions: List[dict]) -> Optional[dict]:
    """严格匹配文件、规则、目标和可选作用域，不允许别名或包前缀吞并。"""
    norm_file = os.path.normpath(rel_path).replace("\\", "/")
    rule = finding["rule"]
    target = finding["target"]
    scope = finding.get("scope", "<module>")

    for exc in exceptions:
        exc_file = os.path.normpath(exc.get("file", "")).replace("\\", "/")
        exc_rule = exc.get("rule", "")
        exc_target = exc.get("target", "")
        exc_scope = exc.get("scope")

        # 文件、规则和目标都必须精确匹配，禁止包前缀和规则别名放宽例外。
        if norm_file != exc_file or rule != exc_rule or target != exc_target:
            continue

        if exc_scope is not None and exc_scope != scope:
            continue

        return exc
    return None


def verify_exception_requirements(
    rel_path: str, tree: ast.AST, matched: dict, finding: dict
) -> Tuple[bool, str]:
    """验证已登记例外的 verification_requirement 是否真正得到满足（可执行门禁检查）。"""
    rule = matched.get("rule", "")
    req = matched.get("verification_requirement", "")
    scope = finding.get("scope", "<module>")
    scope_args = finding.get("scope_args", [])

    # 1. 对 tests/ 目录中跨仓相关的例外，强制验证具体发生违规的作用域绑定了受控临时上下文/fixture
    if (rel_path.startswith("tests/") or rel_path.startswith("dev/tests/")) and rule in (
        "RULE_NO_DIRECT_PARSER_IMPORT", "RULE_DYNAMIC_IMPORT_REGISTERED", "RULE_NO_CROSS_REPO_SYS_PATH"
    ):
        if scope == "<module>":
            return False, "跨仓操作出现在模块顶层 (<module>)，未封装在受控 fixture 或测试用例中"

        ALLOWED_TEMP_FIXTURES = {"tmp_path", "mail_store", "billing_db", "tmpdir", "parser_repo"}
        if not any(arg in ALLOWED_TEMP_FIXTURES for arg in scope_args):
            return False, (
                f"违规所在函数 '{scope}' 未声明受控临时上下文 fixture 参数 "
                f"(必须包含 tmp_path / parser_repo / mail_store 之一，实测参数: {scope_args})"
            )

    # 2. 对 app/plugins.py 受控插件加载器例外，强制验证函数内部包含白名单校验
    norm_file = os.path.normpath(rel_path).replace("\\", "/")
    if norm_file in ("app/plugins.py", "lite_agent/app/plugins.py") and rule == "RULE_DYNAMIC_IMPORT_REGISTERED":
        fn_nodes = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == scope
        ]
        if len(fn_nodes) != 1:
            return False, "受控插件加载器作用域不唯一或缺失"
        body = fn_nodes[0].body
        guarded = False
        for statement in body:
            if isinstance(statement, ast.If):
                test = statement.test
                if (isinstance(test, ast.Compare) and len(test.ops) == 1
                        and isinstance(test.ops[0], ast.NotIn)
                        and isinstance(test.left, ast.Name) and test.left.id == "entry_name"
                        and len(test.comparators) == 1
                        and isinstance(test.comparators[0], ast.Attribute)
                        and isinstance(test.comparators[0].value, ast.Name)
                        and test.comparators[0].value.id == "self"
                        and test.comparators[0].attr == "allowed_entries"
                        and statement.body and isinstance(statement.body[0], ast.Raise)):
                    guarded = True
            for call in ast.walk(statement):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "import_module" and not guarded):
                    return False, "动态加载前缺少无条件 entry_name not in self.allowed_entries 拒绝门禁"
        if not guarded:
            return False, "缺少无条件的白名单拒绝门禁"

    return True, ""


def match_exception_and_verify(
    finding: dict, rel_path: str, tree: ast.AST, exceptions: List[dict]
) -> Tuple[Optional[dict], Optional[str]]:
    matched = match_exception(finding, rel_path, exceptions)
    if not matched:
        return None, None
    ok, reason = verify_exception_requirements(rel_path, tree, matched, finding)
    if not ok:
        return None, reason
    return matched, None


def run_checks(
    repo_root: str,
    config_path: str,
    target_dir: Optional[str] = None,
    explicit_files: Optional[List[str]] = None,
) -> Tuple[int, List[dict], List[dict]]:
    cfg = load_rules_config(config_path)
    exceptions = cfg.get("exceptions", [])

    if explicit_files:
        files = explicit_files
    else:
        files = get_repo_python_files(repo_root, target_dir)

    violations = []
    approved_exceptions = []
    exception_hits = {}

    for rel_path in files:
        abs_path = os.path.join(repo_root, rel_path)
        if not os.path.isfile(abs_path):
            continue
        try:
            with open(abs_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
            tree = ast.parse(content, filename=rel_path)
        except Exception as e:
            violations.append({
                "file": rel_path,
                "line": "0",
                "rule": "PARSE_ERROR",
                "target": "",
                "detail": f"AST 解析失败: {e}",
                "suggestion": "修复 Python 语法错误",
            })
            continue

        visitor = ArchitectureVisitor(rel_path)
        visitor.visit(tree)

        for finding in visitor.findings:
            matched, fail_reason = match_exception_and_verify(finding, rel_path, tree, exceptions)
            item = {
                "file": rel_path,
                "line": finding["line"],
                "rule": finding["rule"],
                "target": finding["target"],
                "detail": finding["detail"],
                "suggestion": finding["suggestion"],
            }
            if matched:
                key = id(matched)
                exception_hits[key] = exception_hits.get(key, 0) + 1
                limit = matched.get("max_occurrences")
                if limit is not None and exception_hits[key] > limit:
                    matched = None
                    fail_reason = f"例外命中超过冻结上限 {limit}"
            if matched:
                item["exception_owner"] = matched.get("owner", "unknown")
                item["removal_stage"] = matched.get("removal_stage", "unknown")
                approved_exceptions.append(item)
            else:
                if fail_reason:
                    item["detail"] = f"{finding['detail']} (门禁校验未通过: {fail_reason})"
                violations.append(item)

    return len(files), violations, approved_exceptions


def main():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    config_path = os.path.join(repo_root, "dev/config/architecture/rules.json")
    if not os.path.exists(config_path):
        config_path = os.path.join(repo_root, "config/architecture/rules.json")

    target_dir = None
    if len(sys.argv) > 1:
        if sys.argv[1] == "--config" and len(sys.argv) > 2:
            config_path = sys.argv[2]
        elif sys.argv[1] == "--target-dir" and len(sys.argv) > 2:
            target_dir = sys.argv[2]

    file_count, violations, approved = run_checks(repo_root, config_path, target_dir)

    print(f"🔍 架构依赖门禁检查完成: 扫描 {file_count} 个 Python 文件")
    print(f"📋 已核准例外项: {len(approved)} 处 (已登记并追踪移除阶段)")

    if violations:
        print(f"\n❌ 发现 {len(violations)} 处架构违规:")
        for v in violations:
            print(f"  - [{v['rule']}] {v['file']}:{v['line']}")
            print(f"    详情: {v['detail']}")
            print(f"    修复建议: {v['suggestion']}")
        sys.exit(1)
    else:
        print("✅ 未发现未登记的架构依赖违规，门禁检查通过！")
        sys.exit(0)


if __name__ == "__main__":
    main()
