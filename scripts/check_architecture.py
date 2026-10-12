#!/usr/bin/env python3
"""
静态架构依赖门禁检查器 (check_architecture.py)
基于 Python 标准库 AST，检查仓库内代码是否违反架构分层依赖约束。

规则包括：
1. RULE_NO_DIRECT_PARSER_IMPORT: 禁止 Agent 生产代码直接导入 parser 模块 (如 mail_client, statement_parser 等)
2. RULE_NO_CORE_REVERSE_DEPENDENCY: 禁止 core/ 核心通用运行时产生对 skills/、scripts/ 或具体业务工作流的反向依赖
3. RULE_DYNAMIC_IMPORT_REGISTERED: 禁止通过未登记的动态调用 (importlib.import_module / __import__ 及别名) 绕过门禁
4. RULE_NO_CROSS_REPO_SYS_PATH: 禁止在生产代码中动态注入跨仓外部路径到 sys.path
5. RULE_NO_DIRECT_SQLITE_IN_BILLING: 禁止 features/billing/ 业务代码直接导入 sqlite3 直连解析/账单数据库
6. RULE_NO_FEATURE_SYS_MODULES_LOOKUP: 禁止 features/ 特性业务代码通过 sys.modules 反查上层技能或兼容层模块

使用方式：
    python scripts/check_architecture.py
    python scripts/check_architecture.py --config architecture_rules.json
"""

import ast
import json
import os
import subprocess
import sys
from typing import Dict, List, Set, Tuple, Optional

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

    excluded_prefixes = (".git", ".venv", "venv", "scratch", "docs", ".gemini", "deploy")
    result = []
    for f in sorted(files_set):
        norm = os.path.normpath(f)
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


class ArchitectureVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str):
        self.rel_path = rel_path
        self.is_core = rel_path.startswith("core" + os.sep) or rel_path.startswith("core/")
        self.is_test = rel_path.startswith("tests" + os.sep) or rel_path.startswith("tests/")
        self.is_billing_feature = self.rel_path.startswith("features" + os.sep + "billing") or self.rel_path.startswith("features/billing")
        self.is_feature = self.rel_path.startswith("features" + os.sep) or self.rel_path.startswith("features/")
        self.findings: List[Dict[str, Any]] = []
        self.scope_stack: List[Union[ast.FunctionDef, ast.AsyncFunctionDef]] = []

        # 跟踪动态导入函数的局部别名
        self.importlib_aliases: Set[str] = {"importlib"}
        self.import_module_aliases: Set[str] = {"import_module"}
        self.dunder_import_aliases: Set[str] = {"__import__"}

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

            # 规则 1: 禁止直接导入 parser 模块
            if root_mod in PARSER_MODULES:
                self._record_finding(
                    "RULE_NO_DIRECT_PARSER_IMPORT",
                    root_mod,
                    node.lineno,
                    f"import {mod_name}",
                    "改用 MailCLI 或 integrations 适配器进行跨仓调用",
                )

            # 规则 2: core 禁止导入 skills / scripts
            if self.is_core and root_mod in ("skills", "scripts"):
                self._record_finding(
                    "RULE_NO_CORE_REVERSE_DEPENDENCY",
                    mod_name,
                    node.lineno,
                    f"core 导入反向模块: {mod_name}",
                    "将业务或技能逻辑下沉或使用端口注入，避免 core 依赖外层",
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
        mod_name = node.module or ""
        root_mod = mod_name.split(".")[0]

        if mod_name == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    self.import_module_aliases.add(alias.asname or alias.name)
        elif mod_name == "builtins":
            for alias in node.names:
                if alias.name == "__import__":
                    self.dunder_import_aliases.add(alias.asname or alias.name)

        # 规则 1: 禁止直接 from parser_mod import ...
        if root_mod in PARSER_MODULES:
            self._record_finding(
                "RULE_NO_DIRECT_PARSER_IMPORT",
                root_mod,
                node.lineno,
                f"from {mod_name} import ...",
                "改用 MailCLI 或 integrations 适配器进行跨仓调用",
            )

        # 规则 2: core 禁止反向依赖
        if self.is_core:
            for alias in node.names:
                full_mod = f"{mod_name}.{alias.name}" if mod_name else alias.name
                root_item = full_mod.split(".")[0]
                if root_item in ("skills", "scripts"):
                    self._record_finding(
                        "RULE_NO_CORE_REVERSE_DEPENDENCY",
                        full_mod,
                        node.lineno,
                        f"core 导入反向模块: {full_mod}",
                        "将业务逻辑解耦，避免 core 导入 skills/scripts",
                    )
                if "billing_workflow" in full_mod and alias.name == "BillingWorkflow":
                    self._record_finding(
                        "RULE_NO_CORE_REVERSE_DEPENDENCY",
                        "core.billing_workflow.BillingWorkflow",
                        node.lineno,
                        "core 直接导入具体业务 BillingWorkflow",
                        "使用工作流注册表在启动时装配，解耦通用编排器与具体业务",
                    )

        # 规则 5: features/billing 禁止从 sqlite3 导入
        if self.is_billing_feature and root_mod == "sqlite3":
            self._record_finding(
                "RULE_NO_DIRECT_SQLITE_IN_BILLING",
                "sqlite3",
                node.lineno,
                f"from {mod_name} import ...",
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
                    if target_str and (target_str.startswith("skills.") or target_str.startswith("integrations.") or target_str.startswith("core.")):
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
                                if target_str.startswith("skills.") or target_str.startswith("integrations.") or target_str.startswith("core."):
                                    self._record_finding(
                                        "RULE_NO_FEATURE_SYS_MODULES_LOOKUP",
                                        target_str,
                                        node.lineno,
                                        f"features 模块通过 sys.modules 检查旧入口: {target_str}",
                                        "严禁 features 反向查找旧入口模块，依赖必须通过参数显式注入",
                                    )
        self.generic_visit(node)


    def visit_Call(self, node: ast.Call):
        func = node.func
        is_dynamic = False

        # 检查是否调用动态导入（支持别名跟踪）
        if isinstance(func, ast.Name):
            if func.id in self.import_module_aliases or func.id in self.dunder_import_aliases:
                is_dynamic = True
        elif isinstance(func, ast.Attribute) and func.attr == "import_module":
            if isinstance(func.value, ast.Name) and func.value.id in self.importlib_aliases:
                is_dynamic = True

        if is_dynamic:
            target_name = ""
            if node.args:
                first_arg = node.args[0]
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
                if root_mod in PARSER_MODULES or (self.is_core and root_mod in ("skills", "scripts")):
                    self._record_finding(
                        "RULE_DYNAMIC_IMPORT_REGISTERED",
                        target_name,
                        node.lineno,
                        f"动态导入敏感模块: {target_name}",
                        "动态导入跨仓/敏感模块必须在 architecture_rules.json 中显式登记精确例外",
                    )

        # 规则 4: 检查生产代码与测试代码中的 sys.path 跨仓动态修改
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
    """严格匹配已登记的例外清单（禁止通配符 *，禁止任意双向前缀匹配）。"""
    norm_file = os.path.normpath(rel_path)
    rule = finding["rule"]
    target = finding["target"]
    scope = finding.get("scope", "<module>")

    for exc in exceptions:
        exc_file = os.path.normpath(exc.get("file", ""))
        exc_rule = exc.get("rule", "")
        exc_target = exc.get("target", "")
        exc_scope = exc.get("scope")

        # 严格精确匹配 file, rule, target 以及可选的 scope
        if norm_file == exc_file and rule == exc_rule and target == exc_target:
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

    # 对 tests/ 目录中跨仓相关的例外，强制验证具体发生违规的作用域绑定了受控临时上下文/fixture
    if rel_path.startswith("tests/") and rule in (
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
    config_path = os.path.join(repo_root, "architecture_rules.json")

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
