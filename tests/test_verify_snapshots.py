"""
Unit tests for create_sanitized_snapshots and build_isolated_docker_image in scripts/verify_e0.py.
Verifies D1, D2, D4 requirements:
- D1: Explicit allowlist prevents copying arbitrary non-.py files in subdirectories,
  such as skills/config.json, skills/mail.eml, skills/statements.db-wal (even without 'private' in name);
- D2: Unconditional realpath and commonpath checks prevent directory/ancestor symlinks from escaping,
  rejects prefix collisions (/repo vs /repo-evil), and cleans up temporary dirs on failure;
- D4: Isolated Docker build context transmits only Dockerfile.test and requirements-test.txt to the builder.
"""

import os
import shutil
import tempfile
from unittest.mock import patch, MagicMock
import pytest

from scripts.verify_e0 import (
    create_sanitized_snapshots,
    validate_safe_path,
    build_isolated_docker_image,
)


def test_d1_agent_snapshot_excludes_unlisted_files_even_without_private_name(tmp_path):
    fake_parser = tmp_path / "fake_parser"
    fake_agent = tmp_path / "fake_agent"
    fake_parser.mkdir()
    fake_agent.mkdir()

    # 基础合法文件
    (fake_parser / "statement_parser.py").write_text("# parser", encoding="utf-8")
    (fake_agent / "agent.py").write_text("# agent", encoding="utf-8")
    (fake_agent / "config.example.json").write_text('{"mode": "example"}', encoding="utf-8")

    skills_dir = fake_agent / "skills"
    skills_dir.mkdir()
    (skills_dir / "ops_mail.py").write_text("# skill code", encoding="utf-8")

    # D1 复现对象: 命名中完全没有 private 字样的非代码敏感/测试产物文件
    (skills_dir / "config.json").write_text('{"db_pass": "LEAKED_PASS"}', encoding="utf-8")
    (skills_dir / "mail.eml").write_text("DRAFT_EMAIL_BODY", encoding="utf-8")
    (skills_dir / "statements.db-wal").write_text("SQLITE_WAL_BYTES", encoding="utf-8")

    tmp_base, agent_snap, parser_snap = create_sanitized_snapshots(str(fake_agent), str(fake_parser))
    try:
        # 验证合法代码被复制
        assert os.path.exists(os.path.join(agent_snap, "skills", "ops_mail.py"))

        # 验证上述三个非代码文件全部被严格白名单机制排除
        assert not os.path.exists(os.path.join(agent_snap, "skills", "config.json")), "skills/config.json 必须被排除"
        assert not os.path.exists(os.path.join(agent_snap, "skills", "mail.eml")), "skills/mail.eml 必须被排除"
        assert not os.path.exists(os.path.join(agent_snap, "skills", "statements.db-wal")), "db-wal sidecar 必须被排除"
    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)


def test_d2_symlink_directory_escaping_repo_root_is_rejected_and_cleaned(tmp_path):
    fake_parser = tmp_path / "fake_parser"
    fake_agent = tmp_path / "fake_agent"
    outside_dir = tmp_path / "outside_dir"
    fake_parser.mkdir()
    fake_agent.mkdir()
    outside_dir.mkdir()

    (outside_dir / "CMB_TEMPLATE_V1.json").write_text('{"secret": "outside"}', encoding="utf-8")
    # parser/rules 是指向外部目录的软链接
    rules_link = fake_parser / "rules"
    rules_link.symlink_to(outside_dir)

    (fake_parser / "statement_parser.py").write_text("# parser", encoding="utf-8")
    (fake_agent / "agent.py").write_text("# agent", encoding="utf-8")
    (fake_agent / "config.example.json").write_text('{}', encoding="utf-8")

    before = set(os.listdir(tempfile.gettempdir()))
    # 捕获异常，断言被安全拒绝
    with pytest.raises(ValueError, match="安全拒绝: 路径逃逸仓库根"):
        create_sanitized_snapshots(str(fake_agent), str(fake_parser))

    # 验证异常发生后没有残留新分配的临时快照目录
    after = set(os.listdir(tempfile.gettempdir()))
    new_dirs = [d for d in (after - before) if d.startswith("lite_agent_e0_sanitized_")]
    assert len(new_dirs) == 0, f"快照生成失败后残留未清理目录: {new_dirs}"


def test_d2_validate_safe_path_prefix_collision_blocked(tmp_path):
    repo_root = tmp_path / "myrepo"
    evil_root = tmp_path / "myrepo-evil"
    repo_root.mkdir()
    evil_root.mkdir()
    evil_file = evil_root / "payload.py"
    evil_file.write_text("evil", encoding="utf-8")

    # 前缀碰撞攻击: /path/to/myrepo-evil 具有 /path/to/myrepo 作为字符串前缀
    with pytest.raises(ValueError, match="安全拒绝: 路径逃逸仓库根"):
        validate_safe_path(str(evil_file), str(repo_root))


def test_d2_symlink_file_and_nested_ancestor_escaping_blocked(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()

    outside_file = outside / "secret.key"
    outside_file.write_text("key", encoding="utf-8")

    # 1. 直接文件软链接指向外部
    link_file = repo / "bad_link.py"
    link_file.symlink_to(outside_file)
    with pytest.raises(ValueError, match="安全拒绝: 路径逃逸仓库根"):
        validate_safe_path(str(link_file), str(repo))

    # 2. 嵌套祖先目录软链接指向外部
    nested_dir = repo / "sub"
    nested_dir.symlink_to(outside)
    nested_target = nested_dir / "secret.key"
    with pytest.raises(ValueError, match="安全拒绝: 路径逃逸仓库根"):
        validate_safe_path(str(nested_target), str(repo))


def test_d4_build_isolated_docker_image_context_is_minimal(tmp_path):
    # 模拟真实 agent 仓库，包含敏感配置
    fake_agent = tmp_path / "agent"
    fake_agent.mkdir()
    (fake_agent / "Dockerfile.test").write_text("FROM python:3.9-slim\n", encoding="utf-8")
    (fake_agent / "requirements-test.txt").write_text("pytest\n", encoding="utf-8")
    (fake_agent / "config.json").write_text('{"api_key": "SENSITIVE"}', encoding="utf-8")
    (fake_agent / ".env").write_text("PASSWORD=12345", encoding="utf-8")

    captured_context_files = []
    captured_context_dir = []

    def mock_run(cmd, *args, **kwargs):
        # 捕获传入 docker build 的最后一个参数 (构建上下文目录)
        context_dir = cmd[-1]
        captured_context_dir.append(context_dir)
        captured_context_files.extend(os.listdir(context_dir))
        mock_res = MagicMock()
        mock_res.returncode = 0
        return mock_res

    with patch("subprocess.run", side_effect=mock_run):
        ok = build_isolated_docker_image(str(fake_agent), "test_tag:3.9")
        assert ok is True

    # 验证传入 builder 的上下文绝对不是原仓库目录
    assert captured_context_dir[0] != str(fake_agent)
    # 验证上下文目录内仅有 Dockerfile.test 和 requirements-test.txt 两个文件
    assert set(captured_context_files) == {"Dockerfile.test", "requirements-test.txt"}
    assert "config.json" not in captured_context_files
    assert ".env" not in captured_context_files
