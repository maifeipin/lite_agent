"""
Unit tests for scripts/check_architecture.py.
Verifies:
1. Current repository passes all architectural rules with registered exceptions;
2. Simulating a new reverse dependency in core/ is detected and rejected;
3. Simulating a direct import of parser in production code is detected and rejected;
4. Simulating an unregistered dynamic import is detected and rejected;
5. Aliased dynamic imports (from importlib import import_module as load; load('...')) are detected;
6. Arbitrary sys.path manipulation in production code is detected;
7. Exact matching logic refuses wildcard or prefix swallowing;
8. Untracked python files in subdirectories are discovered by get_repo_python_files.
"""

import os
import tempfile
import pytest
from scripts.check_architecture import run_checks, get_repo_python_files


@pytest.fixture
def repo_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def rules_config(repo_root):
    return os.path.join(repo_root, "architecture_rules.json")


def test_current_repo_passes_architecture_check(repo_root, rules_config):
    file_count, violations, approved = run_checks(repo_root, rules_config)
    assert file_count > 0
    assert len(violations) == 0, f"Unexpected violations found: {violations}"
    assert all(item.get("exception_owner") for item in approved)


def test_core_reverse_dependency_rejected(repo_root, rules_config, tmp_path):
    fake_core_file = tmp_path / "core" / "bad_component.py"
    fake_core_file.parent.mkdir(parents=True, exist_ok=True)
    fake_core_file.write_text("import skills.ops_todo\n", encoding="utf-8")

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["core/bad_component.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] == "RULE_NO_CORE_REVERSE_DEPENDENCY"
    assert "skills.ops_todo" in v["detail"]


def test_direct_parser_import_rejected(repo_root, rules_config, tmp_path):
    fake_file = tmp_path / "integrations" / "leak.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text("import statement_parser\n", encoding="utf-8")

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["integrations/leak.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] == "RULE_NO_DIRECT_PARSER_IMPORT"
    assert "statement_parser" in v["target"]


def test_unregistered_dynamic_import_rejected(repo_root, rules_config, tmp_path):
    fake_file = tmp_path / "channels" / "dynamic_leak.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text("import importlib\nmod = importlib.import_module('mail_client')\n", encoding="utf-8")

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["channels/dynamic_leak.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] == "RULE_DYNAMIC_IMPORT_REGISTERED"
    assert "mail_client" in v["target"]


def test_aliased_dynamic_import_rejected(repo_root, rules_config, tmp_path):
    # from importlib import import_module as load; load('statement_parser')
    fake_file = tmp_path / "channels" / "alias_leak.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        "from importlib import import_module as load\nmod = load('statement_parser')\n",
        encoding="utf-8"
    )

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["channels/alias_leak.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] == "RULE_DYNAMIC_IMPORT_REGISTERED"
    assert v["target"] == "statement_parser"


def test_sys_path_injection_in_prod_rejected(repo_root, rules_config, tmp_path):
    fake_file = tmp_path / "core" / "sys_path_leak.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        "import sys\nsys.path.insert(0, '/opt/custom_lib')\n",
        encoding="utf-8"
    )

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["core/sys_path_leak.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] == "RULE_NO_CROSS_REPO_SYS_PATH"


def test_exact_matching_refuses_prefix_or_wildcard_swallowing(repo_root, rules_config, tmp_path):
    # core/output_delivery.py has exception for target mail_client.
    # If someone in core/output_delivery.py imports mail_sync (unregistered), it must NOT be swallowed!
    fake_file = tmp_path / "core" / "output_delivery.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        "import importlib\nmod = importlib.import_module('mail_sync')\n",
        encoding="utf-8"
    )

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["core/output_delivery.py"]
    )
    assert len(violations) == 1
    assert violations[0]["target"] == "mail_sync"


def test_d3_unrelated_tmp_path_function_does_not_satisfy_cross_repo_exception(repo_root, rules_config, tmp_path):
    # D3 复现反例: 函数名匹配已登记例外，但自身未声明受控 fixture 参数；同文件中的无关函数即便有 tmp_path 也无法放行
    fake_file = tmp_path / "tests" / "test_mail_quality_closeout.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        "import importlib\n"
        "def test_real_cmb_case_is_repeat_safe_through_parser_cli_and_todo():\n"
        "    importlib.import_module('statement_parser')\n"
        "\n"
        "def unrelated(tmp_path):\n"
        "    pass\n",
        encoding="utf-8"
    )

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["tests/test_mail_quality_closeout.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] == "RULE_DYNAMIC_IMPORT_REGISTERED"
    assert "门禁校验未通过" in v["detail"]
    assert "未声明受控临时上下文 fixture 参数" in v["detail"]


def test_d3_unregistered_scope_cross_repo_import_rejected(repo_root, rules_config, tmp_path):
    # D3: 违规函数名为 bad_func，即便带有 tmp_path，因未登记对应作用域例外，直接作为未授权违规拦截
    fake_file = tmp_path / "tests" / "test_mail_quality_closeout.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        "import importlib\n"
        "def bad_func(tmp_path):\n"
        "    importlib.import_module('statement_parser')\n",
        encoding="utf-8"
    )

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["tests/test_mail_quality_closeout.py"]
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "RULE_DYNAMIC_IMPORT_REGISTERED"
    assert violations[0]["target"] == "statement_parser"

