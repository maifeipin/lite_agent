"""
Phase 0 行为锁定测试 — 共享 fixtures。

隔离策略：
- 保存/恢复全局 _skill_registry，避免测试间污染；
- 不触发 skills/ 目录的真实模块导入（避免 Cron 注册、DB 连接等副作用）；
- 每个测试独立构造 SkillEngine 实例，通过直接操作 registry 注册测试技能。
"""

import sys
import pytest
from core.skill_engine import _skill_registry, skill, SkillEngine


@pytest.fixture(autouse=True)
def _isolate_skill_registry():
    """每个测试前后保存/恢复全局 _skill_registry，防止测试间污染。"""
    saved = dict(_skill_registry)
    _skill_registry.clear()
    yield
    _skill_registry.clear()
    _skill_registry.update(saved)


@pytest.fixture
def engine():
    """返回一个干净的 SkillEngine 实例（不扫描 skills/ 目录）。"""
    # 传入一个不存在的目录，阻止 import 真实技能模块
    return SkillEngine(skills_dir="/tmp/__nonexistent_skills__")


@pytest.fixture
def engine_with_test_skills():
    """返回一个 SkillEngine，注册了四个纯测试技能。"""
    # 先注册测试技能到全局 registry
    _skill_registry.clear()

    @skill(
        name="test_echo",
        description="回显输入",
        params={"text": {"type": "string", "description": "要回显的文本"}},
        side_effect=False,
        dry_run_handler=lambda text: f"echo: {text}",
    )
    def test_echo(text: str) -> str:
        return f"echo: {text}"

    @skill(
        name="test_raise",
        description="总是抛出异常",
        params={"msg": {"type": "string", "description": "异常消息"}},
        side_effect=None,  # 未知
    )
    def test_raise(msg: str = "boom") -> str:
        raise RuntimeError(msg)

    @skill(
        name="test_guest_ok",
        description="访客可用的技能",
        params={},
        guest_ok=True,
        side_effect=False,
        dry_run_handler=lambda: "guest allowed",
    )
    def test_guest_ok() -> str:
        return "guest allowed"

    @skill(
        name="test_write",
        description="写入操作（有副作用，支持 dry-run）",
        params={"data": {"type": "string", "description": "要写入的数据"}},
        side_effect=True,
        dry_run_handler=lambda data: f"[dry-run] would write: {data}",
    )
    def test_write(data: str) -> str:
        return f"wrote: {data}"

    return SkillEngine(skills_dir="/tmp/__nonexistent_skills__")